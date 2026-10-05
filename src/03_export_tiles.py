"""
Step 3: Export GeoParquet, convert for tippecanoe, build PMTiles.

Why GeoParquet as the interchange format: typed, compressed, geometry stored
as WKB with CRS metadata, readable by DuckDB, GeoPandas, QGIS, and GDAL.

Tippecanoe does not read GeoParquet directly, so we hand it newline-delimited
GeoJSON generated from the GeoParquet by DuckDB's GDAL writer.

Why PMTiles: a single static file served via HTTP range requests. No tile
server, no database: hosting is S3/R2/GitHub Pages.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ATTR_IN = DATA / "h3_hazard.parquet"
GEOPARQUET = DATA / "hex_hazard.geoparquet"
GEOJSONL = DATA / "hex_hazard.geojsonl"
PMTILES = DATA / "hex_hazard.pmtiles"


def main() -> None:
    if not ATTR_IN.exists():
        raise SystemExit("Run src/02_hazard_join.py first.")

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL h3 FROM community; LOAD h3;")

    # 1) GeoParquet export (hexagon geometry materialized from the H3 id).
    con.execute(f"""
        COPY (
            SELECT h3_cell, building_count, total_footprint_m2, hazard_class, hazard_rank,
                   ST_GeomFromText(h3_cell_to_boundary_wkt(h3_string_to_h3(h3_cell))) AS geometry
            FROM read_parquet('{ATTR_IN.as_posix()}')
        ) TO '{GEOPARQUET.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD);
    """)
    print(f"Wrote {GEOPARQUET}")

    # 2) GeoParquet -> GeoJSONSeq (tippecanoe input).
    GEOJSONL.unlink(missing_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM read_parquet('{GEOPARQUET.as_posix()}'))
        TO '{GEOJSONL.as_posix()}' (FORMAT GDAL, DRIVER 'GeoJSONSeq');
    """)

    # 3) Tippecanoe -> PMTiles.
    if not shutil.which("tippecanoe"):
        sys.exit("tippecanoe not found on PATH. Install it (see README), then re-run this script.")
    cmd = [
        "tippecanoe",
        "-o", str(PMTILES),
        "-l", "hexagons",              # layer name used by the frontend
        "-zg",                          # auto-pick max zoom
        "-P",                           # parallel read of line-delimited input
        "--coalesce-densest-as-needed",
        "--extend-zooms-if-still-dropping",
        "--force",
        str(GEOJSONL),
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"Wrote {PMTILES} ({PMTILES.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()