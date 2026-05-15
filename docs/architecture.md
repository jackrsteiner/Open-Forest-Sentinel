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
