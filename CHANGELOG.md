# Changelog

## 0.3.0 (unreleased)

The reservoir-engineering model: a fourth model kind, `res`, with its own agent, review and approval.

**Upgrade: a fresh database.** SCAL, rock compressibility, contacts and the region arrays moved from the geo model
to the res model, and SQLite can't change the existing tables in place. `opmagent` refuses a 0.2 database and says
so; move it aside and run `opmagent init-db` (or `opmagent setup <workspace>`), then rebuild the studies.
No approvals are carried over. Design: [docs/design/res-model.md](docs/design/res-model.md).

- **Res model** (`res_models`, `res_model_versions`, `res_properties`; `swof`, `sgof`, `rock`, `contacts`,
  `aquifers`, `aquifer_connections` now belong to it): the dynamic half of the reservoir model, defined on one geo
  version (`geo_version_id`, required). Region arrays SATNUM, EQLNUM, FIPNUM, ROCKNUM and SWATINIT moved with it;
  PVTNUM stays in geo. The lifecycle triggers, approval gate and review feedback work for `res` like the others.
- **Geo model** is the static model: grid, cell properties, PVTNUM, faults. The geo pipeline no longer writes SCAL,
  rock or contacts (`contacts` in the geo spec is optional, for the HCPV in its summary only); its QC notebook drops
  the contacts and SCAL checks.
- **Res modeler** agent (`res` profile, which also reads geo models) and `res-modeler` skill.
  `opmagent res build --spec --geo-version --name`, `opmagent study build res`: Corey per rock type or measured
  tables, rock, contacts per EQLNUM, region arrays by geo zone or layer range. Validator: region arrays against the
  geo grid, SWOF/SGOF rules per SATNUM, contacts per EQLNUM, aquifer connections. Static QC page for now.
- **Decks take four approved models**: `decks.res_version_id`; a trigger checks it is approved, has the deck's unit
  system and is defined on the deck's geo version. The deck builder merges geo and res (`render.combine`).
- **Studies**: chain `data -> fluid, geo -> res (after geo), well -> deck -> run -> view`; model name `<study>_Res`.
  Contacts are the res modeler's input: their data issues and the contacts-vs-SW-log check are reported for `res`.
- **Reverse process and importer** produce four models; the deck comparison compares the res tables too. SPE1,
  SPE9 and Norne (reverse study and importer round trip) pass unchanged. `hello` builds four demo models.
- The well QC notebook takes the contacts from the res model on its geo version.
- **`opmagent doctor` shows each agent's condition** (`opmagent/doctor.py`): installed, skill current, its MCP
  servers in `.mcp.json` and approved by Claude Code (`--deep`: started over stdio, tools listed), its profile,
  last activity and failed writes (`audit_log`), and its work (study tasks, open requests, models waiting for
  review, runs, runs whose process is gone). Fails if the reviewer server is in the agents' workspace.
  `-v`, `--json`, `--workspace`. The `opm-doctor` skill runs it from Claude Code (`/opm-doctor`, or "how are the
  agents?") and reports what needs attention with the fix.
- **Static view of a geo model in ResInsight** (`opmagent/geo/static_view.py`): the static model as a deck (the shared
  GRID section renderer, placeholder PROPS/SOLUTION so OPM loads it, the res model's region arrays and SWATINIT when
  one exists on that geo, a deck-sourced study's GRID passthrough) made into EGRID + INIT by an OPM Flow dry run.
  Made after every geo build (never fails the build), for drafts too; `opmagent results open --geo <id>`,
  `opmagent geo view <id>`, MCP `static_view` (geo, view, reviewer profiles); `study view` shows it. SPE9 and Norne
  static views match their own decks' INIT (PORV, PERMX, TRANX/Y/Z, FIPNUM) exactly. Not registered as a deck.
  A res build remakes its geo's static view with the region arrays; `results open --geo` remakes it when the res
  model changed. Testing scenario `testing/geo-static-view.md` (+ `geo_static_view.sh`, `geo_static_check.py`):
  raw data, SPE9 and Norne, compared with each deck's own INIT, then opened in ResInsight.
- **Initialisation view of a deck** (`opmagent/deck/init_view.py`): the dynamic model at time zero. The deck is
  copied with one 1e-4 day step and no wells (SUMMARY: FOIP, FGIP, FWIP, FPR and their region vectors) and run in
  OPM Flow; report step 0 is the initial state for ResInsight. Made after every deck build (never fails it); fluids in
  place per field and FIPNUM region, initial pressure range, hydrocarbon pore-volume fraction, and checks against the
  res model's contacts per EQLNUM (cells below the OWC at the table's maximum Sw, n/a with capillary pressure at
  maximum Sw; no free gas below the GOC). `opmagent deck init`, `results open --deck <id> --init`, MCP `init_view`
  (deck, run, view, reviewer); `study view` lists it. Testing scenario `testing/init-view.md` (+ `init_view.sh`,
  `init_view_check.py`): hello, then SPE9 and Norne rebuilt through studies, whose initial state equals their own
  decks' (Norne: all 16 regions).
- **ResInsight outside the workspace**: without `[viewer] resinsight_bin`, the viewer looks on PATH (`resinsight`,
  `ResInsight`), then in `/opt/ResInsight` and `~/.local/opt/ResInsight`; `opmagent setup` no longer looks inside the
  workspace, and `opmagent doctor` shows where ResInsight is. README: install it in `/opt/ResInsight`, with a wrapper in
  `/usr/local/bin` (the launcher can't be symlinked). The testing scripts rely on the same lookup (`RESINSIGHT=` overrides).
- Runs always use the release Flow: a `flow_bin` pointing at the Debug build (`opm_debug_flow`) is refused.

## 0.2.0 (unreleased)

QC notebooks for the fluid modeler.

- **Standard QC notebook** (`opmagent/notebooks/fluid_qc.ipynb`). It is now the fluid QC report: each build
  writes a payload, runs the template in a fresh kernel and registers the HTML export as the version's report.
  The approval stays tied to that file's sha256, and the executed `.ipynb` and the payload are registered with
  it. Its findings cover lab data, lab vs model and the black-oil tables as stored. They go into `qc_json`
  metrics (`qc_overall`, `qc_findings`, `qc_notebook_sha256`). A failing cell leaves the version a draft.
- **Investigation notebooks**: `create_investigation_notebook` / `run_investigation_notebook` (fluid profile)
  and `opmagent fluid investigate | run-notebook`. They are registered as supporting evidence.
- `run_qc_notebook` / `opmagent fluid qc`: re-run the standard QC for a draft and get a new report to resubmit.
- DL lab-vs-model errors are compared on the lab's own steps; DL is path dependent.
- `[qc] report = "notebook" | "html"`, `[qc] timeout_s`. `engine.phase_envelope`, `engine.components`.
- New dependencies: nbformat, nbclient, nbconvert, ipykernel.
- **Geo QC notebook** (`opmagent/notebooks/geo_qc.ipynb`, checks in `geo_qc_lib`): the geo QC report is now a
  notebook as well, kept in `qc/geo_<model>_v<N>/` with the payload and the logs the build used. It reads the
  cell arrays from their registered files (sha256 checked) and the tables from the database. Findings:
  surfaces vs well tops and pinched zones; model vs upscaled logs at the wells, layers without logs, leave one
  well out; PERMY/PERMZ vs the spec, clip limits, NTG, mean porosity vs logs; contacts vs the spec and the grid;
  volumes recomputed from the arrays; SWOF/SGOF end points. Plots: surfaces with tops, layer maps, sections
  through a well, each well's logs against its model column, distributions, poro-perm.
  The notebook tools are on the geo profile too, with `opmagent geo qc | investigate | run-notebook`; the geo
  agent gets NotebookEdit. With `[qc] report = "html"` the static report is kept. Models from a deck keep
  their model view.
- **Well QC notebook** (`opmagent/notebooks/well_qc.ipynb`, checks in `well_qc_lib`), kept in
  `qc/well_<model>_v<N>/` with the measured history (`history.csv`). Findings: heads vs surveys, wells inside the
  geo grid, station spacing, dogleg severity; perforations open before a well flows and, with a geo version,
  placed in the grid as the deck builder does (length outside the layers, tight cells, producers below the OWC
  or above the GOC); the volume the deck applies vs the measured volume per well and phase (downtime: WCONHIST
  applies volume / producing days for the whole month), missing months, spikes, missing BHP; events before the
  start, prediction overlapping the history, targets far above the last rates. Plots: plan view, perforated
  cells, sections through the zones, rates and cumulatives vs measured, the schedule.
  `opmagent well qc | investigate | run-notebook`; the well agent gets NotebookEdit.

Studies: the agents work through one simulation question together.

- **Study board** (`studies`, `study_tasks`, `study_requests`, `study_events`, added to existing databases on
  first use): a fixed task chain data -> fluid, geo, well -> deck -> run -> view. A task is ready when its
  dependencies are done; completing it checks its result (approved model version, validated deck, finished run).
  Requests let an agent ask another for missing or better data; the asking task is blocked until resolved and a
  finished task is reopened. `opmagent study init | list | status | events | build fluid|geo|well|deck`.
- **Coordinator**: a `study` skill for your own session hands each agent its ready task and routes requests and
  confirmations; it never writes data itself.
- **Data manager** agent (`data` profile):
  - recognises a study's inbox by content: lab report workbooks, LAS, tops and well tables in CSV or any Excel sheet, Volve production data, geo.json;
  - maps column-name variants;
  - converts the **tabular** data into the study data tables. Each row points at its source file and sheet in `data_sources`, and what is still missing goes to `data_issues`.
  - **File-native** data stays a file, as it came, so other software can open it: LAS, SEG-Y, surfaces, grids, documents and images. Every original is filed in `<study>/data/<category>/` and catalogued in `data_files` (format, checksum, well, metadata). Each LAS curve's unit, depth range and min/mean/max go to `data_log_curves`; the samples stay in the file.
  - The loaded data is checked with the modelers' own readers. Missing items are reported, never filled in.
  - `opmagent data check | load | show | verify`, MCP `load_study_data`.

  The study data tables are `data_pvt_samples`, `_composition` and `_tests`; `data_tops`; `data_geo_spec`; `data_contacts`; `data_wells`; `data_surveys`; `data_perforations`; `data_controls`; and `data_production`.
- **Study data layer** (`opmagent.data.store`): one way to read a study's data, whether it is a table or a file.
  - It finds a log in the catalog, checks the file is still the one that was loaded, and reads it with the format's reader (`opmagent.data.formats`: LAS now; SEG-Y catalogued). Changed or missing files are refused until reloaded.
  - `verify_study_files`, `opmagent data verify`.
- **Combined checks** (`opmagent.data.checks`) run with every load and as `check_study_data`:
  - tops vs porosity coverage of the reservoir interval
  - perforations vs surveyed depth
  - OWC vs water saturation logs
  - production vs well types
- **Reverse process: a study from an existing deck.** The data manager reads a deck into the study's data: `opmagent data from-deck <study> <deck> [--check]`, MCP `load_deck_data`.
  - Tables go to the database: PVT, SCAL, rock, contacts, faults, wells, groups, IJK completions and the schedule (`data_deck_rows`, exact numbers).
  - The grid and cell arrays stay files in `<study>/data/deck/load-NNN/`, catalogued. Each load gets its own folder.
  - The model headers, deck spec and coverage go to `data_deck`.
  - The modelers build normal versions from it with `opmagent study build`, going through their validators and the approval gate. The deck builder renders the source deck's dates, schedule and passthrough keywords.
  - `opmagent study compare <study>` runs the source deck and checks the study's run reproduces it: static, initial state and every summary vector.
  - Tested through whole studies on the hello deck, a deck built forward from raw data, SPE1, SPE9 and Norne.
  - **Deck comparison** (`opmagent/deck/deck_compare.py`, `opmagent deck compare A B`): are two decks the same simulation deck? Both are read by the OPM parser: grid, every table row, every cell array as OPM processes it, the schedule (dates, report steps, verbatim keywords). Output-only keywords (RPTRST, ...) are listed, not failed. `opmagent study compare` now compares the decks first, then the runs; `--deck-only` needs no run.
  - **Model views before simulation:** a deck-sourced model's QC report now plots what it holds: PVTO/PVDG curves and the water and density tables; per-layer maps, histograms and layer means of each cell array, SWOF/SGOF, contacts and rock; the well map on the grid, completion intervals and the schedule's controls over time. `opmagent study view <study> [--open]` lists or opens them.
  - **Input from another study:** `opmagent data from-study <study> <source>`, MCP `load_study_input` (data manager). Tables are copied row for row; the grid and array files are copied into the study's own folder after an integrity check. The deck is not read again.
  - **The board from a terminal:** `opmagent study task <study> <task> start|complete`, `opmagent study build ... --complete`, `opmagent study run <study>` (runs the study's deck through the simulation service and completes the run task).
  - **Testing scenarios** in `testing/`: the SPE9 reverse loop (two connected scenarios, success criteria, troubleshooting) and `testing/spe9_loop.sh`.
  - Fixed: `opmagent results open --dry-run` crashed when ResInsight wasn't found.
  - A deck holds the modelers' results, not their raw data. So a study from a deck rebuilds that deck as given; it doesn't re-derive lab data, logs or tops.
- **Modelers read through the data layer.** New read tools: `study_data`, `study_data_rows`, `log_stats` (catalog), `log_samples` (from the files), `fluid_sample`, `study_files`, `check_study_data`.
  - The modelers use them for their analysis and interpretation.
  - `opmagent study build fluid|geo|well` builds from the tables. The version records the inbox files the data came from.
  - The geo and well pipelines also take DataFrames.
- **Review feedback** (`review_feedback`):
  - The reviewer comments on a model version, item by item: `opmagent review comment`, MCP `add_review_comment`. A rejection's comment is recorded there too.
  - The modeler reads the open comments (`get_review_feedback`). It answers each one as addressed, with the version that addresses it, or wontfix with the reason (`answer_review_feedback`).
  - Comments can't be edited. `opmagent review feedback` lists them with the answers, and the study board shows open comments.
- **Results viewer** agent (`view` profile): `opmagent results open --study | --run | --case` opens a run in
  ResInsight (`[viewer] resinsight_bin`).
- **Optional auto-approve** (`[study] auto_approve = true`): a model that passes its validator is approved
  automatically, recorded as `auto (approval disabled)`. Default off: approval works as in 0.1.
- Study tools on every agent's data-server profile, acting on that agent's own task only:
  `study_status`, `list_studies`, `start_task`, `complete_task`, `fail_task`, `retry_task`, `raise_request`,
  `resolve_request`; `init_study` for the deck builder.
- Modeler results report the real review state (approved automatically, or waiting for review).

Upgrade: `opmagent setup <workspace>` adds the two agents, their MCP servers, the skills and the new
`CLAUDE.md` (it rewrites the agent files, skills and CLAUDE.md; your `opmagent.toml` is kept: add `[study]`
and `[viewer]` from `opmagent.example.toml`).

## 0.1.0

Fluid, geo and well modelers, deck builder, run agent (live progress, `opmagent runs watch`), deck importer with
bit-exact round trip (SPE1, SPE9, Norne), human approval gate, `hello` demo, workspace setup, DBeaver monitoring.
