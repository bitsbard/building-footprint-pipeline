# Cloud-Native Building Footprint Pipeline

A serverless geospatial ETL pipeline that turns Overture Maps building footprints into an H3-indexed, hazard-joined vector tileset, with no database server.

**Stack:** DuckDB (Spatial, httpfs, H3) · Overture Maps GeoParquet on S3 · Uber H3 · Tippecanoe · PMTiles · MapLibre GL JS

## Architecture: Decoupled Compute + Cloud-Native Storage

```
Overture GeoParquet (S3)
   │  HTTP range requests, bbox pushdown (no full download)
   ▼
DuckDB (embedded compute) ── centroid → H3 res 9 → GROUP BY
   │
   ├─ join with hazard polygons (GeoJSON) + benchmark
   ▼
GeoParquet ── GeoJSONSeq ── tippecanoe ──► .pmtiles (static file)
                                               │ HTTP range requests
                                               ▼
                                     MapLibre GL JS (static site)
```

- **Storage is files**: Parquet on S3 in, GeoParquet/PMTiles out. Nothing to provision, patch, or back up.
- **Compute is ephemeral**: DuckDB runs in-process on a laptop, CI job, or container.
- **Why GeoParquet**: columnar and compressed, with bbox statistics that let DuckDB skip irrelevant row groups. Only the needed columns and ranges are fetched.
- **Why H3**: building centroids are snapped to hexagon ids so the heavy part of the analysis is an integer `GROUP BY`. The polygon-vs-polygon join then runs on thousands of hexagons rather than every building footprint, which is where traditional PostGIS workflows spend most of their time.
- **Why PMTiles**: one static file served with range requests, so no tile server is required.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Install [tippecanoe](https://github.com/felt/tippecanoe) (v2.17+ for PMTiles output):

```bash
brew install tippecanoe        # macOS
# Linux/Windows: build from source (WSL on Windows)
```

## Run

```bash
python src/01_ingest_and_h3.py   # S3 -> H3 aggregates (data/h3_agg.parquet)
python src/02_hazard_join.py     # mock hazard layer, join, benchmark
python src/03_export_tiles.py    # GeoParquet + PMTiles
npx http-server . -p 8080 --cors # PMTiles needs HTTP range support
# open http://localhost:8080/frontend/
```

Pin an Overture release with `OVERTURE_RELEASE=<version> python src/01_ingest_and_h3.py`. The default is the latest release from Overture's STAC catalog.

Note: `python -m http.server` does not support range requests, so use `http-server` or similar.

## Notes and limitations

- The wildfire hazard layer is **synthetic**. Replace `ZONES` in `02_hazard_join.py` with real data (e.g. CAL FIRE FHSZ) for real analysis.
- Footprint area is computed in UTM 10N (EPSG:32610), which suits Santa Cruz. Change it for other regions.
- Benchmark results depend on hardware and network. Record your own numbers and compare against a PostGIS instance on the same machine.

## Resume bullets

- Built a serverless geospatial ETL pipeline using DuckDB Spatial to query Overture Maps GeoParquet directly on S3 with bounding-box pushdown, avoiding a full dataset download or any database server.
- Indexed building footprints to Uber H3 (resolution 9) and aggregated count and footprint area per hexagon, replacing polygon-on-polygon joins with integer-key aggregation.
- Joined H3 aggregates to a wildfire hazard layer with a spatial join and wrote a benchmark harness comparing DuckDB timings against a PostGIS baseline [add your measured results].
- Exported results to GeoParquet and generated PMTiles with Tippecanoe, served as a static file to a MapLibre GL JS dashboard with no tile server.
- Designed the architecture around decoupled compute and cloud-native storage, so the pipeline runs with no managed infrastructure.