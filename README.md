# Cloud-Native Building Footprint Pipeline

A serverless geospatial ETL pipeline that turns Overture Maps building footprints into an H3-indexed, hazard-joined vector tileset, with no database server.

**Stack:** DuckDB (Spatial, httpfs, H3) · Overture Maps GeoParquet on S3 · Uber H3 · Tippecanoe · PMTiles · MapLibre GL JS

## Architecture: Decoupled Compute + Cloud-Native Storage

```mermaid
flowchart LR
    subgraph STORAGE_IN["Cloud-Native Storage (input)"]
        S3[("Overture Maps<br/>GeoParquet on S3<br/>theme=buildings")]
    end

    subgraph COMPUTE["Ephemeral Compute (local / CI / container)"]
        direction TB
        S1["01_ingest_and_h3.py<br/>bbox pushdown, centroid,<br/>H3 res 9, GROUP BY"]
        S2["02_hazard_join.py<br/>hazard GeoJSON,<br/>spatial join, benchmark"]
        S3X["03_export_tiles.py<br/>GeoParquet, GeoJSONSeq,<br/>tippecanoe"]
        S1 --> S2 --> S3X
    end

    subgraph STORAGE_OUT["Cloud-Native Storage (output)"]
        PQ[("h3_agg.parquet<br/>h3_hazard.parquet<br/>hex_hazard.geoparquet")]
        PM[("hex_hazard.pmtiles<br/>static file")]
    end

    subgraph CLIENT["Static Frontend"]
        MAP["MapLibre GL JS<br/>+ pmtiles protocol"]
    end

    S3 -- "HTTP range requests<br/>(no full download)" --> S1
    S1 --> PQ
    PQ --> S2
    S3X --> PM
    PM -- "HTTP range requests" --> MAP
```

- **Storage is files:** Parquet on S3 in, GeoParquet and PMTiles out. Nothing to provision, patch, or back up.
- **Compute is ephemeral:** DuckDB runs in-process on a laptop, CI job, or container, and disappears when done.
- **Why GeoParquet:** columnar and compressed, with bbox statistics that let DuckDB skip irrelevant row groups and fetch only the needed columns and byte ranges.
- **Why H3:** building centroids are snapped to hexagon ids, so the heavy part of the analysis is an integer `GROUP BY`. The polygon-on-polygon join then runs on thousands of hexagons instead of every building footprint, which is where traditional PostGIS workflows spend most of their time.
- **Why PMTiles:** one static file served with range requests, so no tile server is required.

## Setup

Run each block one at a time, in order, from the project root (`cloud-native-geo/`).

**1. Create a virtual environment.** Isolates the project's Python dependencies from your system Python.

```bash
python -m venv .venv
```

**2. Activate it.** Makes `python` and `pip` point at the virtual environment. On Windows use `.venv\Scripts\activate` instead.

```bash
source .venv/bin/activate
```

**3. Install Python dependencies.** Installs DuckDB. The `spatial`, `httpfs`, and `h3` extensions are downloaded by the scripts on first run.

```bash
pip install -r requirements.txt
```

**4. Install tippecanoe (v2.17+).** Required by step 3 of the pipeline to build PMTiles. On Linux, build from [source](https://github.com/felt/tippecanoe). On Windows, use WSL.

```bash
brew install tippecanoe
```

## Run

Run each block one at a time, in order.

**1. Ingest and aggregate.** Queries Overture's public S3 bucket with a Santa Cruz bounding box, so only the matching byte ranges are read. Converts each building centroid to an H3 res 9 cell and aggregates building count and footprint area per cell. Writes `data/h3_agg.parquet`.

```bash
python src/01_ingest_and_h3.py
```

To pin a specific Overture release instead of using the latest:

```bash
OVERTURE_RELEASE=<version> python src/01_ingest_and_h3.py
```

**2. Join hazard zones and benchmark.** Writes a synthetic wildfire hazard GeoJSON, spatially joins it to the H3 hexagons, times the join over several runs, and writes `data/h3_hazard.parquet`.

```bash
python src/02_hazard_join.py
```

**3. Export tiles.** Writes `data/hex_hazard.geoparquet`, converts it to GeoJSONSeq, then runs tippecanoe to produce `data/hex_hazard.pmtiles`.

```bash
python src/03_export_tiles.py
```

**4. Serve the project.** Starts a local static server from the project root. PMTiles needs HTTP range request support, so `python -m http.server` will not work here.

```bash
npx http-server . -p 8080 --cors
```

**5. Open the map.** Visit this URL in your browser. Use the dropdown to switch between hazard class and building count coloring, and click a hexagon for details.

```text
http://localhost:8080/frontend/
```