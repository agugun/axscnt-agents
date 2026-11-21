# Design: reservoir-engineering model (`res`) and the res-modeler agent

Status: proposal, for review before implementation. Branch: `feat/res-model` (from `feat/geo-qc-notebook`).

## Why

The geo model holds two kinds of data today, reviewed and approved as one unit:

| Static (geologist, petrophysicist) | Dynamic (reservoir engineer) |
| --- | --- |
| grid (COORD/ZCORN or DX/DY/DZ/TOPS), ACTNUM, MINPV, PINCH | relative permeability and capillary pressure (SWOF, SGOF) |
| PORO, PERMX/Y/Z, NTG, MULT* | rock compressibility (ROCK) |
| faults, fault multipliers | initialisation: contacts, datum depth and pressure (EQUIL) |
| | region arrays (SATNUM, EQLNUM, FIPNUM, ROCKNUM), SWATINIT |
| | analytical aquifers and their connections |

The dynamic half has no owner that builds it from data: SCAL is a Corey curve from four numbers typed into
the geo spec, rock compressibility and the initial pressure come from the same spec, regions and SWATINIT
only arrive through an imported deck, and aquifers have tables and validator rules but nothing builds them
and the deck builder cannot write them (they only survive as passthrough text). Whoever approves the grid
also approves the relative permeability, usually without the data behind it.

## What changes

A fourth model kind, **`res`**, with its own versions, review, QC notebook, data-server profile and agent
(`res-modeler`). The geo model keeps the static half. A deck is built from four approved versions.

### Tables

| Table | Today | After |
| --- | --- | --- |
| `swof`, `sgof` | geo | **res** |
| `rock` | geo | **res** |
| `contacts` | geo | **res** |
| `aquifers`, `aquifer_connections` | geo | **res** |
| region arrays and SWATINIT (`geo_properties` keywords SATNUM, EQLNUM, FIPNUM, ROCKNUM, SWATINIT) | geo | **res** (`res_properties`, same shape: constant or array file) |
| PVTNUM | geo | stays geo for now (it selects fluid tables; it moves when the fluid model gets regions) |
| `faults`, `fault_multipliers`, grid, cell properties | geo | geo |

New: `res_models`, `res_model_versions` (header: `unit_system`, `geo_version_id` NOT NULL, `spec_json`,
`assumptions_json`, `qc_json`, `qc_report_file_id`, `origin`, `source_deck_file_id`, status lifecycle as the
other kinds), `res_version_sources`, `res_properties`. The moved tables get `version_id` referencing
`res_model_versions`. The generated lifecycle triggers (data locked once a version leaves draft, approval
only through `approvals`) are generated for `res` like the other kinds.

`decks` gets `res_version_id` (nullable for decks built before this change; required for new decks by the
deck builder and by a trigger that checks it is approved, as for the other three). The geo, well and res
versions of a deck must share the unit system; the res version's `geo_version_id` must be the deck's geo
version.

Every `CHECK (… IN ('fluid','geo','well'))` gains `'res'`: `approvals.model_kind`,
`review_feedback.model_kind`, `data_deck_rows.kind`, `data_issues.modeler`, `study_tasks.task`.

### Study chain

```
data -> fluid, geo -> res (after geo), well (after geo) -> deck (fluid, geo, res, well) -> run -> view
```

The res model needs the grid (regions by zone, EQUIL datum inside the grid, aquifer connections on grid
faces), so it waits for geo, like well.

### Res-modeler inputs

- **SCAL**: a new data-manager input, a SCAL workbook (template like the lab report): per rock type, either
  measured kr / Pc points (Sw, krw, krow, Pcow; Sg, krg, krog, Pcog) or Corey parameters (Swc, Sorw, Sorg,
  Sgc, exponents, end points) with the source stated. One rock type per SATNUM.
- **Contacts and initial pressure**: `data_contacts` as today (moves from the geo spec to the res spec);
  several equilibration regions allowed.
- **Rock**: compressibility and reference pressure per region.
- **Regions**: rule-based in the spec: SATNUM / EQLNUM / FIPNUM by zone (geo zone layers) or by a property
  cut-off (e.g. PERMX classes), written as arrays on the geo grid.
- **Aquifers**: Fetkovich or Carter-Tracy parameters and the grid faces they connect to (e.g. "J- face of
  zone Lower, rows 1-3"), written to `aquifers` / `aquifer_connections`.
- **SWATINIT** (optional): from the SW logs through the same upscaling as geo, or omitted.

`opmagent res build --spec res.json --geo-version N --name X [--scal scal.xlsx]` and
`opmagent study build res <study>`.

### Deck builder

Reads SCAL, rock, contacts, regions, SWATINIT and aquifers from the res version instead of geo. New
rendering for aquifers: `AQUDIMS` (RUNSPEC), `AQUFETP` / `AQUCT` + `AQUTAB` as needed, `AQUANCON` (SOLUTION).
Until then aquifers in imported decks keep coming through passthrough, unchanged.

### Reverse process and importer

`import_deck` and `deck_source` produce four models: the dynamic tables and region arrays go to res. The SPE1,
SPE9 and Norne round trips and the SPE9 reverse loop must still pass unchanged (the deck comparison compares
the parsed decks, so where a table lives in the database doesn't change the result).

### QC

`res_qc.ipynb` (same mechanism as fluid, geo and well): kr curves and end points per SATNUM against the SCAL
data, Pc curves, SWOF/SGOF consistency (kro at Swc), Sw from Pc against the SW logs (if any), region maps and
volumes per region, EQUIL datum and contacts against the grid, initial pressure gradient against the fluid
densities, aquifer volume and productivity against the reservoir pore volume, connections on active faces.

### What it costs (estimate)

| Part | Work |
| --- | --- |
| Schema, triggers, kind registry, migration | medium (table rebuilds for the CHECK constraints) |
| Data server profile, review, studies chain, setup, agent, skill | small-medium |
| Geo pipeline sheds the dynamic half; res pipeline; hello demo builds four models | medium |
| Deck builder reads res; aquifer rendering | medium |
| Importer, deck_source, deck_compare split four ways | medium-large (round trips must stay green) |
| SCAL workbook template and loader (data manager) | medium |
| Res QC notebook | medium |
| Tests and docs | medium |

### Phases (each phase leaves the suite green)

1. **Core**: kind `res`, tables, triggers, migration, `decks.res_version_id`, profile, review, study chain.
2. **Builders**: res pipeline (Corey or tables, contacts, rock, regions), geo stops writing the dynamic half,
   hello demo and tests on four models, deck builder reads res.
3. **Reverse**: importer, deck_source, deck_compare on four models; SPE1/SPE9/Norne round trips.
4. **Inputs and QC**: SCAL workbook and loader, aquifers (build and render), res QC notebook.
5. **Agent and docs**: res-modeler agent and skill, study skill, README, CHANGELOG, testing scenario update.

## Decisions (2026-11-21)

1. **Existing databases: a fresh database.** 0.3 is a new schema. `opmagent` refuses a database without the
   res tables and says to create a new workspace database (`opmagent setup` / `opmagent init-db`); no approvals
   are carried over or invented. Old studies are re-run.
2. **Region arrays move to res**: SATNUM, EQLNUM, FIPNUM, ROCKNUM and SWATINIT. PVTNUM stays in geo.
3. **Scope of `feat/res-model`: phases 1-3** (the split working end to end with the current inputs, round trips
   green). The SCAL workbook, aquifer building and rendering, and the res QC notebook follow in a later branch;
   until then the res QC report is a static page and aquifers in imported decks stay passthrough.
