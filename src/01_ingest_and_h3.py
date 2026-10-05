"""
Step 1: Remote GeoParquet ingestion + H3 aggregation.

Zero-copy: DuckDB reads only the Parquet row groups / column chunks it needs
from Overture's public S3 bucket via HTTP range requests. We never download
the full buildings theme (~hundreds of millions of rows).

Why GeoParquet: columnar, compressed, carries per-row-group min/max stats on
the `bbox` struct, so the bbox filter below is pushed down and skips most of
the planet without reading it. A PostGIS equivalent needs a full load + GiST index first.

Why H3: polygon-on-polygon joins are the expensive part of PostGIS workloads.
Snapping each building centroid to an H3 cell turns "which area is this
building in" into an integer GROUP BY, and shrinks millions of footprints to
a few thousand hexagons before any geometry predicate runs.
"""
import json
import os
import time
from pathlib import Path
from urllib.request import urlopen

import duckdb

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "h3_agg.parquet"

BBOX = {"xmin": -122.3, "ymin": 36.8, "xmax": -121.7, "ymax": 37.3}  # Santa Cruz, CA
H3_RES = 9
UTM_CRS = "EPSG:32610"  # UTM 10N, metric CRS for Santa Cruz (area in m^2)


def overture_release() -> str:
    if os.getenv("OVERTURE_RELEASE"):
        return os.environ["OVERTURE_RELEASE"]
    with urlopen("https://stac.overturemaps.org/catalog.json", timeout=30) as r:
        return json.load(r)["latest"]


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    con.execute("SET s3_region = 'us-west-2';")  # public bucket, anonymous access
    return con


def main() -> None:
    release = overture_release()
    src = f"s3://overturemaps-us-west-2/release/{release}/theme=buildings/type=building/*"
    print(f"Overture release: {release}")

    con = connect()
    sql = f"""
    COPY (
        WITH buildings AS (
            SELECT id, geometry
            FROM read_parquet('{src}', hive_partitioning = 1)
            WHERE bbox.xmin >= {BBOX['xmin']} AND bbox.xmax <= {BBOX['xmax']}
              AND bbox.ymin >= {BBOX['ymin']} AND bbox.ymax <= {BBOX['ymax']}
        ),
        measured AS (
            SELECT
                ST_Centroid(geometry) AS c,
                ST_Area(ST_Transform(geometry, 'EPSG:4326', '{UTM_CRS}', always_xy := true)) AS area_m2
            FROM buildings
        )
        SELECT
            h3_h3_to_string(h3_latlng_to_cell(ST_Y(c), ST_X(c), {H3_RES})) AS h3_cell,
            COUNT(*)                AS building_count,
            ROUND(SUM(area_m2), 1)  AS total_footprint_m2
        FROM measured
        GROUP BY 1
    ) TO '{OUT.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD);
    """
    t0 = time.perf_counter()
    con.execute(sql)
    elapsed = time.perf_counter() - t0

    n, b = con.execute(
        f"SELECT COUNT(*), SUM(building_count) FROM read_parquet('{OUT.as_posix()}')"
    ).fetchone()
    print(f"{n:,} H3 cells (res {H3_RES}) from {b:,} buildings in {elapsed:.1f}s -> {OUT}")


if __name__ == "__main__":
    main()