# Work plan

This work plan organizes implementation around the pipeline and domain described in the README. It does not invent milestones beyond what the README specifies; ordering, scope, and unknowns reflect the README directly.

Work is structured as **epics**, each containing **beads** (small, agent-sized units of work). See `docs/beads.md` for what a bead is and how to file one. The agent-bead issue template is at `.github/ISSUE_TEMPLATE/agent-bead.yml`.

## Guiding properties

Every epic and every bead must respect:

- **AOI-first.** New code paths must work for an arbitrary configured AOI, not just the Solomon Islands.
- **Low cost.** Solutions stay within free-tier / low-cost envelopes for reasonably sized AOIs.
- **Temporal currency.** Designs preserve the ability to refresh detections more often than weekly.
- **Provenance.** Every derived artifact is traceable to its source observations and to a `methodology_version`.

## Epics

The epics below mirror the pipeline and stack described in the README. Each one becomes a tracking issue with sub-issue beads.

### E1 — Project foundations
Set up the repository so beads can be implemented, tested, and shipped.

- Python project layout, dependency management, lint / format / type configuration.
- Test harness and CI on GitHub Actions for pull requests.
- Documentation conventions (this file, `docs/architecture.md`, `docs/beads.md`).

**Acceptance:** a trivial Python module can be added, tested, and shipped through CI.

### E2 — AOI configuration
Make AOI deployability a first-class, code-free configuration surface.

- Load a configured AOI geometry (per README step 3).
- Validate AOI metadata (name, geometry, CRS, etc. — fields **TBD**).
- Persist AOI records in PostGIS (`aoi` domain object).

**Acceptance:** the pipeline can be pointed at an arbitrary AOI without code changes.

### E3 — HLS imagery access
Access relevant HLS analysis-ready imagery for a configured AOI (README step 4).

- Discover HLS scenes intersecting the AOI for a time window.
- Record `observation`s with sensor, timestamp, cloud / quality metadata, and source scene identifiers.
- Handle availability / cloud-cover gaps without breaking the pipeline.

**Acceptance:** for any configured AOI, the pipeline can enumerate and ingest the relevant HLS observations.

### E4 — Index rasters (NBR, NDVI)
Compute per-observation vegetation / disturbance indices (README step 5).

- `NBR  = (NIR - SWIR2) / (NIR + SWIR2)`
- `NDVI = (NIR - RED)  / (NIR + RED)`
- Write outputs as Cloud Optimized GeoTIFFs (README step 10).
- Record `index_raster` metadata in PostGIS.

**Acceptance:** for each `observation`, NBR and NDVI COGs are produced and indexed.

### E5 — Change products
Compute change products such as ΔNBR / ΔNDVI or other anomaly measures (README step 6).

- Produce `change_raster`s as COGs.
- Record provenance back to source `observation`s and `index_raster`s.

**Acceptance:** for an AOI with sufficient observations, change rasters are produced on schedule.

### E6 — Disturbance candidates
Convert change signals into candidate disturbance polygons (README step 7).

- Persist `disturbance_candidate`s in PostGIS with geometry and provenance.
- Detection thresholds and algorithm: **TBD** in beads.

**Acceptance:** the pipeline emits candidate polygons for real change signals over a test AOI.

### E7 — Event tracking
Track candidate polygons over time as disturbance events (README step 8).

- Maintain `disturbance_event`s spanning multiple dates.
- Capture per-date measurements as `event_observation`s (area, severity, growth).
- Tracking algorithm: **TBD** in beads.

**Acceptance:** repeated runs over the same AOI produce a stable, growing record of events with per-date measurements.

### E8 — Manual review
Allow humans to validate detections (README domain object `manual_review`).

- Record validation, notes, uncertainty, false-positive status.
- Surface review state in the dashboard.

**Acceptance:** a reviewer can mark an event reviewed, false-positive, or uncertain, and the result persists.

### E9 — Methodology versioning
Tag every derived artifact with the processing / detection method that produced it (README domain object `methodology_version`).

**Acceptance:** every `index_raster`, `change_raster`, `disturbance_candidate`, and `disturbance_event` references a `methodology_version`.

### E10 — Dashboard
Deliver the lightweight web dashboard, backed by PostGIS (README §"Product Deliverable", step 9, and stack).

- Maps, timelines, event detail views, AOI summary metrics.
- Surfaces: where, when first detected, size, expansion rate, status (new / ongoing / resolved / uncertain), supporting evidence.
- Framework choice: **TBD**.

**Acceptance:** a user can answer all six questions listed in the README "Product Deliverable" section from the dashboard.

### E11 — Scheduled execution
Run the end-to-end pipeline on a cron schedule on a Google Compute Engine VM, triggered by GitHub Actions (README steps 1–2).

- Cron trigger configuration.
- VM provisioning / runner wiring.
- Run logging and failure handling.

**Acceptance:** scheduled runs refresh the dashboard without manual intervention.

### E12 — Raster storage layout
Store COGs in the prototype location (`/data/cogs/` on the VM) with a layout that allows a future move to Google Cloud Storage without code changes outside a storage abstraction.

**Acceptance:** the pipeline writes COGs to the prototype location, and the storage interface is the only place that needs to change to switch to GCS.

### E13 — Database (PostgreSQL + PostGIS)
Stand up PostgreSQL + PostGIS on the GCE VM and persist the domain objects from the README.

- Schemas for `aoi`, `observation`, `index_raster`, `change_raster`, `disturbance_candidate`, `disturbance_event`, `event_observation`, `manual_review`, `methodology_version`.
- Migration / versioning approach: **TBD**.
- Future managed path: Cloud SQL for PostgreSQL with PostGIS.

**Acceptance:** all domain objects are persisted in PostGIS and queryable by the pipeline and dashboard.

## Dependency map

The pipeline imposes a natural ordering. Each arrow reads "is required by".

```
E1 Foundations ──► E2 AOI ──► E3 HLS imagery ──► E4 Index rasters ──► E5 Change ──► E6 Candidates ──► E7 Events ──► E10 Dashboard
                                                                                                       │
E13 Database ─────────────────────────────────────────────────────────────────────────────────────────►┤
E12 Raster storage ───────────────────────────────────────────────────────────────────────────────────►┤
E9  Methodology versioning ───────────────────────────────────────────────────────────────────────────►┤
E8  Manual review ────────────────────────────────────────────────────────────────────────────────────►┤
E11 Scheduled execution wraps E2–E10 once they exist.
```

Beads inside each epic must record their dependencies on beads in upstream epics using `Depends on #NNN` references, per `docs/beads.md`.

## Open questions

These are points the README does not resolve. They should be answered inside the relevant epic and recorded in `docs/architecture.md` once decided.

- Concrete table schemas for each domain object.
- Detection thresholds and the candidate-polygon extraction algorithm.
- The polygon-tracking algorithm used to assemble events from candidates.
- Dashboard framework and hosting.
- Migration tooling for PostgreSQL + PostGIS.
- Authentication / access model for the dashboard and review workflows.
- Retention policy for COGs and observations.
