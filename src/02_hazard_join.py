"""
Step 2: Mock wildfire hazard layer, spatial join against H3 cells, benchmark.

The hazard layer is SYNTHETIC (hand-drawn polygons over the Santa Cruz
Mountains), for demonstration only. Swap in CAL FIRE FHSZ data for real use.

Benchmark notes (DuckDB vs. a traditional PostGIS setup):
  * PostGIS joining raw building polygons to hazard polygons needs a GiST index
    on both tables, and every candidate pair gets an exact ST_Intersects test.
    Cost scales with building count and polygon vertex count.
  * Here we join ~thousands of H3 hexagons (already aggregated) to a handful
    of hazard polygons, so the geometry predicate runs on orders of magnitude
    fewer rows. DuckDB also uses a vectorized, multi-threaded engine and an
    R-tree spatial join operator in recent versions.
  * There is no server, no load step, no index build: the inputs are files.
  * Fairness: PostGIS wins for transactional workloads, concurrent writers,
    and repeated indexed lookups. This pipeline is optimized for analytical,
    batch, read-heavy work. Run the same join in your own PostGIS instance to
    get a real comparison on your hardware; don't trust generic numbers.
"""
import json
import statistics
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
H3_IN = DATA / "h3_agg.parquet"
HAZARD = DATA / "wildfire_hazard.geojson"
OUT = DATA / "h3_hazard.parquet"
RUNS = 5

# (lng, lat) rings. Mock zones, NOT real fire hazard data.
ZONES = [
    ("Moderate", 1, [[-121.95, 36.95], [-121.75, 36.95], [-121.75, 37.05], [-121.95, 37.05], [-121.95, 36.95]]),
    ("High", 2, [[-122.15, 36.97], [-121.95, 36.97], [-121.95, 37.05], [-122.20, 37.05], [-122.15, 36.97]]),
    ("Very High", 3, [[-122.20, 37.05], [-121.95, 37.05], [-121.90, 37.20], [-122.15, 37.25], [-122.20, 37.05]]),
]


def write_mock_hazard() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"risk_class": name, "risk_rank": rank},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
            for name, rank, ring in ZONES
        ],
    }
    HAZARD.write_text(json.dumps(fc))


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    return con


JOIN_SQL = f"""
CREATE OR REPLACE TABLE joined AS
WITH hexes AS (
    -- Rebuild hexagon polygons from the cell id; geometry is only materialized here.
    SELECT h3_cell, building_count, total_footprint_m2,
           ST_GeomFromText(h3_cell_to_boundary_wkt(h3_string_to_h3(h3_cell))) AS hex_geom
    FROM read_parquet('{H3_IN.as_posix()}')
)
SELECT
    h.h3_cell,
    h.building_count,
    h.total_footprint_m2,
    COALESCE(arg_max(z.risk_class, z.risk_rank), 'None') AS hazard_class,
    COALESCE(max(z.risk_rank), 0)                        AS hazard_rank
FROM hexes h
LEFT JOIN hazard z ON ST_Intersects(h.hex_geom, z.geom)
GROUP BY h.h3_cell, h.building_count, h.total_footprint_m2
"""


def main() -> None:
    if not H3_IN.exists():
        raise SystemExit("Run src/01_ingest_and_h3.py first.")
    write_mock_hazard()

    con = connect()
    con.execute(f"CREATE TEMP TABLE hazard AS SELECT risk_class, risk_rank, geom FROM ST_Read('{HAZARD.as_posix()}')")

    # Sanity check: H3 boundary WKT must be lng/lat order, or the join silently returns nothing.
    x = con.execute(
        f"SELECT ST_X(ST_PointN(ST_ExteriorRing(ST_GeomFromText(h3_cell_to_boundary_wkt(h3_string_to_h3(h3_cell)))), 1)) "
        f"FROM read_parquet('{H3_IN.as_posix()}') LIMIT 1"
    ).fetchone()[0]
    if not -180 <= x <= -100:
        raise SystemExit(f"H3 boundary looks lat/lng swapped (x={x}); flip coordinates before joining.")

    con.execute(JOIN_SQL)  # warm-up (extension load, file cache)
    times = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        con.execute(JOIN_SQL)
        times.append(time.perf_counter() - t0)

    cells = con.execute("SELECT COUNT(*) FROM joined").fetchone()[0]
    print(f"Join over {cells:,} H3 cells x {len(ZONES)} hazard zones")
    print(f"  min {min(times)*1000:.0f} ms | median {statistics.median(times)*1000:.0f} ms | max {max(times)*1000:.0f} ms ({RUNS} runs)")
    print(con.execute(
        "SELECT hazard_class, COUNT(*) AS cells, SUM(building_count) AS buildings FROM joined GROUP BY 1 ORDER BY MAX(hazard_rank) DESC"
    ).fetchall())

    con.execute(f"COPY joined TO '{OUT.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()