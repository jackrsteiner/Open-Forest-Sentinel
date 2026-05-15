# Architecture

This document describes the architecture of Open Forest Sentinel as defined by the project README. It is intentionally faithful to that source; design choices not stated in the README are flagged as **TBD**.

## 1. Purpose and shape of the system

Open Forest Sentinel is a generalized, low-cost forest disturbance monitoring system for a configurable Area of Interest (AOI). The initial deployment target is the Solomon Islands, but **AOI deployability is a first-class feature**: the same system runs over other countries, regions, protected areas, watersheds, concessions, or custom polygons through configuration rather than code changes.

For an appropriately constrained AOI, the system targets near-zero or very low infrastructure cost, because free-tier and low-cost cloud resources are sufficient for compute, database, and prototype raster storage.

A defining property is **observation currency**: by using openly available HLS imagery with frequent Landsat / Sentinel revisit cadence, detections should be less than one week old and refreshed more frequently than weekly, subject to cloud cover, data availability, and AOI size.

## 2. User-facing deliverable

The product is **not** a set of derived raster files. It is a lightweight dashboard that surfaces:

- where likely logging or forest disturbance is happening
- when disturbance was first detected
- how large the affected area is
- how quickly the disturbance is expanding
- which detections are new, ongoing, resolved, or uncertain
- what satellite-derived evidence supports each detection

Derived rasters are internal analytical artifacts that power detection, tracking, visualization, and review.

## 3. Data pipeline

The pipeline runs on a schedule end-to-end:

1. **GitHub Actions** runs on a cron schedule and triggers the pipeline.
2. A **Google Compute Engine VM** executes the Python processing job.
3. The pipeline loads the configured AOI geometry.
4. The pipeline accesses relevant **HLS analysis-ready imagery**.
5. Python raster modules compute vegetation / disturbance indices:
   - `NBR  = (NIR - SWIR2) / (NIR + SWIR2)`
   - `NDVI = (NIR - RED)  / (NIR + RED)`
6. Change products are computed, such as ΔNBR / ΔNDVI or other anomaly measures.
7. Change signals are converted into candidate disturbance polygons.
8. Candidate polygons are tracked over time as disturbance events.
9. Outputs are exposed through a dashboard with maps, timelines, event detail views, and AOI summary metrics.
10. Raster artifacts are written as **Cloud Optimized GeoTIFFs (COGs)**.
11. Metadata, provenance, AOIs, detections, and event histories live in **PostgreSQL + PostGIS**.

```
schedule (GitHub Actions cron)
        │
        ▼
GCE VM ── load AOI ── fetch HLS ── compute indices (NBR, NDVI)
                                      │
                                      ▼
                          compute change products (ΔNBR, ΔNDVI, anomalies)
                                      │
                                      ▼
                          extract candidate disturbance polygons
                                      │
                                      ▼
                          track polygons → disturbance events
                                      │
                ┌─────────────────────┴─────────────────────┐
                ▼                                           ▼
        COGs on disk / GCS                          PostgreSQL + PostGIS
                                                            │
                                                            ▼
                                                        Dashboard
```

## 4. Prototype technology stack

| Concern                       | Prototype                                                | Future path                                    |
|-------------------------------|----------------------------------------------------------|------------------------------------------------|
| Scheduler / trigger           | GitHub Actions cron                                      | —                                              |
| Compute                       | Google Compute Engine VM                                 | —                                              |
| Database                      | PostgreSQL + PostGIS on the same Compute Engine VM       | Cloud SQL for PostgreSQL with PostGIS          |
| Database access / migrations  | SQLAlchemy 2.0 ORM, GeoAlchemy2 spatial types, Alembic   | —                                              |
| Language                      | Python                                                   | —                                              |
| Raster processing             | rasterio, GDAL, numpy, rio-cogeo                         | —                                              |
| Imagery source                | NASA HLS                                                 | —                                              |
| Raster output format          | Cloud Optimized GeoTIFF                                  | —                                              |
| Raster storage                | Local VM filesystem, e.g. `/data/cogs/`                  | Google Cloud Storage                           |
| Dashboard                     | Lightweight web application backed by PostGIS            | —                                              |
| Versioning / CI               | GitHub                                                   | —                                              |

The prototype is co-located on a single GCE VM (compute + database + raster storage) for cost. The future path separates raster storage to GCS and the database to managed Cloud SQL.

Schema changes are versioned with **Alembic**; each migration is reviewed and shipped in the bead that introduces the schema it depends on. A `docker-compose.yml` at the repository root runs PostgreSQL + PostGIS for local development, and the database URL is supplied through the `FOREST_SENTINEL_DATABASE_URL` environment variable.

## 5. Core domain objects

These are the entities the system tracks. Concrete schemas are recorded in §5.1 as the beads that introduce them ship.

| Object                  | Description                                                                              |
|-------------------------|------------------------------------------------------------------------------------------|
| `aoi`                   | Configured area of interest geometry and metadata.                                       |
| `observation`           | One imagery acquisition / date used for analysis. Holds sensor, timestamp, cloud / quality metadata, and source scene identifiers. |
| `index_raster`          | Derived NBR / NDVI raster metadata.                                                      |
| `change_raster`         | ΔNBR / ΔNDVI or anomaly raster metadata.                                                 |
| `disturbance_candidate` | Raw detected disturbance polygon.                                                        |
| `disturbance_event`     | Tracked logging / disturbance event over time.                                           |
| `event_observation`     | Per-date measurement of event area, severity, and growth.                                |
| `manual_review`         | Human validation, notes, uncertainty, false-positive status.                             |
| `methodology_version`   | Processing and detection method provenance.                                              |

Relationships implied by the pipeline:

- An `aoi` has many `observation`s.
- An `observation` produces `index_raster`s; pairs / sequences of observations produce `change_raster`s.
- A `change_raster` yields `disturbance_candidate`s.
- `disturbance_candidate`s are tracked over time into `disturbance_event`s.
- A `disturbance_event` has many `event_observation`s and may have `manual_review`s.
- Every derived artifact is tagged with the `methodology_version` that produced it.

### 5.1 Concrete schemas

Each entry lands in the bead that introduces the table.

#### `observation` (introduced by bead #37)

One imagery acquisition over an AOI — the source record every derived artifact traces back to. Source data, not a derived artifact, so it carries no `methodology_version` reference.

| Column                | Type          | Notes                                                       |
|-----------------------|---------------|-------------------------------------------------------------|
| `id`                  | `integer`     | Primary key.                                                |
| `aoi_id`              | `integer`     | Foreign key → `aoi.id`.                                     |
| `sensor`              | `text`        | HLS short name, e.g. `HLSL30` or `HLSS30`.                  |
| `acquired_at`         | `timestamptz` | Scene acquisition timestamp.                                |
| `source_scene_id`     | `text`        | Provider scene identifier (e.g. HLS granule id).            |
| `cloud_cover_percent` | `float`       | Optional scene-level cloud cover, when reported.            |
| `created_at`          | `timestamptz` | Row insertion time (server default `now()`).                |

Constraints and indexes: `UNIQUE (aoi_id, source_scene_id)` so re-running HLS discovery is idempotent per AOI; `INDEX (aoi_id, acquired_at)` for "observations for this AOI in this time window" queries.

#### `methodology_version` (introduced by bead #35)

A processing/detection method record. Every derived artifact (`index_raster`, `change_raster`, `disturbance_candidate`) references one of these rows so the inputs and parameters that produced it can always be reconstructed.

| Column       | Type          | Notes                                                        |
|--------------|---------------|--------------------------------------------------------------|
| `id`         | `integer`     | Primary key.                                                 |
| `name`       | `text`        | Method identifier, e.g. `optical-change`.                    |
| `version`    | `text`        | Method version string, e.g. `0.1`.                           |
| `parameters` | `jsonb`       | Method parameters; empty object allowed.                     |
| `created_at` | `timestamptz` | Row insertion time (server default `now()`).                 |

Constraints: `UNIQUE (name, version)`. A `(name, version)` identity is bound to its parameters — `get_or_create_methodology_version` raises `MethodologyVersionMismatch` rather than create a divergent row, so methodology versions stay stable provenance records.

### 5.1.1 HLS imagery access (introduced by bead #38)

NASA HLS analysis-ready imagery (collections `HLSL30` for Landsat 8/9 and `HLSS30` for Sentinel-2, v2.0) is the optical-change input.

- **Library:** `earthaccess` — NASA's official Earthdata client — for CMR search and authenticated access. Discovery (CMR search) is auth-free; authenticated band access is added later when indices are computed.
- **Module:** `forest_sentinel.hls` exposes `discover_observations(session, aoi, *, since, until)` which searches both HLS short names over the AOI's bounding box, parses each granule's UMM into a small `HlsGranule`, and records new `observation` rows. Re-runs are idempotent: the `observation` `(aoi_id, source_scene_id)` unique constraint dedupes.
- **Earthdata Login (auth):** required only for reading band assets, not discovery. The downstream beads (E4 indices, …) will read credentials from environment variables (`EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`, or `EARTHDATA_TOKEN`), or from a `~/.netrc` entry — the standard `earthaccess` discovery order.
- **Tests:** never make live NASA calls; CI and local runs stub `earthaccess.search_data` and parse synthetic UMM payloads.

#### `index_raster` (introduced by bead #39)

A derived NBR or NDVI raster computed for one `observation` under a specific `methodology_version`. The COG itself lives under the storage root (§5.2); the row records its provenance and location.

| Column                   | Type          | Notes                                                       |
|--------------------------|---------------|-------------------------------------------------------------|
| `id`                     | `integer`     | Primary key.                                                |
| `observation_id`         | `integer`     | Foreign key → `observation.id`.                             |
| `methodology_version_id` | `integer`     | Foreign key → `methodology_version.id`.                     |
| `index_type`             | `text`        | `nbr` or `ndvi`.                                            |
| `cog_path`               | `text`        | Storage path returned by `Storage.write_cog`.               |
| `created_at`             | `timestamptz` | Row insertion time (server default `now()`).                |

Constraints: `UNIQUE (observation_id, index_type, methodology_version_id)` so re-running index computation for the same observation under the same methodology updates the existing row instead of duplicating it.

Index math, with HLS surface reflectance in the [-9999 → fill, else int16 × 0.0001 = reflectance] convention:

- `NBR  = (NIR  - SWIR2) / (NIR  + SWIR2)` (HLSL30 NIR=B05/SWIR2=B07; HLSS30 NIR=B8A/SWIR2=B12).
- `NDVI = (NIR  - RED)   / (NIR  + RED)`.

Fill pixels become `NaN`; zero denominators become `NaN`; `NaN` propagates so downstream change detection can ignore missing pixels honestly. Band reading is decoupled through a `BandResolver` protocol so production code uses an HLS-aware (authenticated) resolver while tests use a local-file resolver against fixtures.

#### `change_raster` and `change_raster_source` (introduced by bead #40)

A ΔNBR or ΔNDVI raster: current observation's index minus a per-pixel **trailing-median baseline** built from prior valid observations under the same methodology. Provenance back to every contributing `index_raster` (the current one plus the baseline window) is captured in a `change_raster_source` join table.

`change_raster`:

| Column                   | Type          | Notes                                                       |
|--------------------------|---------------|-------------------------------------------------------------|
| `id`                     | `integer`     | Primary key.                                                |
| `observation_id`         | `integer`     | Foreign key → `observation.id` (the *current* observation). |
| `methodology_version_id` | `integer`     | Foreign key → `methodology_version.id`.                     |
| `change_type`            | `text`        | `delta_nbr` or `delta_ndvi`.                                |
| `cog_path`               | `text`        | Storage path of the delta COG.                              |
| `created_at`             | `timestamptz` | Row insertion time (server default `now()`).                |

Unique: `(observation_id, change_type, methodology_version_id)`.

`change_raster_source` (many-to-many provenance, primary key on the pair):

| Column             | Type      | Notes                                              |
|--------------------|-----------|----------------------------------------------------|
| `change_raster_id` | `integer` | Foreign key → `change_raster.id` (`ON DELETE CASCADE`). |
| `index_raster_id`  | `integer` | Foreign key → `index_raster.id`.                   |

Algorithm:

- **Baseline window size** is configured via the methodology's `baseline_window` parameter (default `5`). The window contains the *prior* observations only, ordered by `acquired_at` descending and limited to N.
- For each requested index type (`nbr`, `ndvi`), read the current and baseline COGs (must share shape/transform/CRS), compute `baseline = np.nanmedian(stack, axis=0)`, then `delta = current - baseline`, and write the delta as a float COG (NaN nodata).
- Re-running the same `(observation, change_type, methodology)` upserts: the COG and `change_raster_source` rows are replaced, not duplicated.
- A fresh AOI with no prior observations produces no change rasters yet — the index type is skipped silently. The next run with one more observation produces deltas.

#### `disturbance_candidate` (introduced by bead #41)

A polygon extracted from a `change_raster`: a candidate forest disturbance. Each candidate carries provenance back to the change raster it was extracted from and to the methodology version that produced it (which records the exact thresholds in its `parameters`).

| Column                   | Type                  | Notes                                          |
|--------------------------|-----------------------|------------------------------------------------|
| `id`                     | `integer`             | Primary key.                                   |
| `change_raster_id`       | `integer`             | Foreign key → `change_raster.id`.              |
| `methodology_version_id` | `integer`             | Foreign key → `methodology_version.id`.        |
| `geometry`               | `geometry(POLYGON, 4326)` | Polygon stored in WGS 84.                   |
| `detected_at`            | `date`                | Acquisition date of the source observation.    |
| `area_m2`                | `float`               | Polygon area in square metres (native CRS).    |
| `created_at`             | `timestamptz`         | Row insertion time (server default `now()`).   |

Index: `(change_raster_id)` so candidates can be enumerated per change raster quickly.

Algorithm:

- Read the change raster; build a binary mask of pixels where `delta <= delta_nbr_threshold` (an NBR drop of at least `-threshold`). NaN pixels never satisfy the comparison and so are excluded automatically.
- Polygonize the mask with `rasterio.features.shapes`, keeping only shapes with `value == 1`.
- Drop polygons smaller than `min_area_m2`, computed in the raster's native (projected) CRS so the area is in real square metres.
- Reproject surviving polygons to WGS 84 with `pyproj.Transformer.from_crs(..., 4326)` and persist.
- Re-runs with the same `(change_raster, methodology)` delete and re-insert candidates so the row set reflects the latest parameters.

Defaults (configurable via the methodology's `parameters`):

| Parameter             | Default     | Meaning                                                |
|-----------------------|-------------|--------------------------------------------------------|
| `delta_nbr_threshold` | `-0.25`     | NBR drop required to flag a pixel as disturbed.        |
| `min_area_m2`         | `4_500`     | Minimum patch area (≈ 0.45 ha; ≈ 50 m × 90 m).         |

### 5.1.2 Slice 1 pipeline orchestration (introduced by bead #42)

`forest-sentinel run --aoi PATH --since DATE --until DATE [--band-root PATH]` threads the Slice 1 stages end-to-end inside a single transactional session:

1. **AOI** — load the GeoJSON config; reuse the existing `aoi` row by name, or persist a new one. The CLI is idempotent at the AOI level.
2. **Methodology** — `get_or_create_methodology_version` resolves the row that derived artifacts will reference; defaults are `optical-change` / `0.1`.
3. **Discovery** — `discover_observations` searches HLS via `earthaccess` and records new `observation`s (no auth required for CMR search).
4. **Index → change → candidate** — when `--band-root` is supplied, iterate observations in `acquired_at` order and run `compute_indices_for_observation` → `compute_change_products_for_observation` → `extract_candidates_for_change_raster`. Candidate extraction is **NBR-driven** (bead #41); ΔNDVI is kept as supporting evidence but does not directly emit candidates.

AOI bboxes are passed everywhere in **WGS 84**. The `read_band_window` helper reprojects the bbox to the band raster's native CRS (typically UTM for HLS) before windowing, so the AOI–HLS-tile join is correct regardless of zone. The CLI prints a per-stage summary and is the Slice 1 hallway test: re-running over a small AOI produces eyeball-able `disturbance_candidate` polygons in PostGIS (`SELECT ST_AsGeoJSON(geometry) …`).

### 5.2 Raster storage (introduced by bead #36)

Index and change rasters are written as Cloud Optimized GeoTIFFs (COGs) through a small storage interface so the backend can be swapped without touching pipeline code.

- **Interface:** `forest_sentinel.storage.Storage` — `path_for(key)` and `write_cog(key, data, transform, crs, nodata)`. `Storage` is a `typing.Protocol`; one implementation today (`LocalStorage`), with Google Cloud Storage as the future path.
- **Layout:** `{root}/{aoi}/{product}/{YYYY-MM-DD}/{filename}`. Free-form `aoi` and `product` names are sanitized to a safe path component (alphanumerics, `-`, `_`); other characters become `_`.
- **Root:** configurable via the `FOREST_SENTINEL_COG_ROOT` environment variable; defaults to `data/cogs/` (relative).
- **COG production:** `rasterio` stages an in-memory GeoTIFF; `rio-cogeo` translates it to a conformant COG (tiled, with overviews, IFD-ordered) under the DEFLATE profile.

## 6. Cross-cutting properties

- **AOI-first configurability.** Switching deployment to a new AOI is a configuration change, not a code change.
- **Cost discipline.** Compute, database, and raster storage choices are bounded by free-tier / low-cost envelopes for reasonably sized AOIs. Cost scales primarily with AOI size, processing frequency, output retention, raster storage volume, and dashboard usage.
- **Temporal currency.** Scheduling and sensor revisit cadence are designed so detections refresh more often than weekly for small-to-medium AOIs.
- **Provenance.** Every derived artifact is traceable to its source observations and to the `methodology_version` that produced it.

## 7. Out of scope (for this document)

Anything not asserted by the README is out of scope here. In particular: detection algorithm thresholds, polygon-tracking algorithm, dashboard framework choice, authentication model, and concrete database schemas. These are **TBD** and will be settled in implementation beads under the relevant epics.
