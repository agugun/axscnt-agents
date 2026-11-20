# Changelog

## 0.2.0 (unreleased)

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
