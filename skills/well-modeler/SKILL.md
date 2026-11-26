---
name: well-modeler
description: Build a well model (wells, trajectories, perforations, production/injection history, prediction controls) from CSV or Excel inputs, including the Volve daily production workbook, and submit it for human review. Use when asked to build or update a well / production model.
---

# Well modeler

Inputs (CSV or Excel, column names case-insensitive; see `opmagent/well/ingest.py`):
- **wells**: well_name, type (PRODUCER/INJECTOR), phase (OIL/WATER/GAS), group, x, y, ref_depth
- **surveys**: well_name, md and either x, y, tvd or inc, azi (minimum curvature; x0/y0 = wellhead)
- **perforations**: well_name, date, top_md, bottom_md, state, skin, diameter
- **history**: well_name, date, oil, water, gas, water_inj, gas_inj, bhp, thp, hours (daily or monthly);
  Volve's daily sheet works directly with `--history-format volve`
- **controls** (prediction): well_name, date, control, rate, bhp
- **groups** (optional): group, parent

## Workflow
1. Find the files (`find_inbox_file` by spoken name). Ask for anything required that's missing:
   wells, surveys and perforations are required; history or controls are recommended.
2. `opmagent well build --wells W --surveys S --perforations P [--history H [--history-format volve]]
   [--controls C] [--groups G] --name <Name> [--start-date YYYY-MM-DD] [--geo-version <geo version id>]`
   Pass the geo version when there is one: the QC then places the perforations in its grid.
3. Read the result: `qc_ready` → tell the user it's waiting for review (wells, perforations, history months,
   QC report path). Validator errors → explain each (e.g. "producer needs a BHP limit in prediction") and
   ask for the fix; don't invent numbers.
   - `qc_overall` is `warn` or `fail` → read `qc_summary.json` next to the report, tell the user each finding
     in one line, and investigate before they review (see QC notebooks).
   - `status: draft` with `qc_notebook` → a notebook cell failed; report it, don't work around it.
4. History is aggregated to monthly average rates (volume / producing days); say so if asked.
5. Perforations are stored by MD; the deck builder places them in grid cells (regular grids and grids from
   the geo modeler).

Never approve; never edit rates by hand.

## QC notebooks
The QC report is the executed standard notebook (`well_qc.html` in `qc/well_<model>_v<N>/`, with
`history.csv`, the measured history as it came in). It reads the model from the database and checks:
- well heads against the surveys, wells inside the geo grid, survey station spacing, dogleg severity;
- perforations open before each well first flows, overlapping records; with a geo version, each perforation
  placed in the grid as the deck builder does: length outside the layers, tight cells, producers perforated
  below the OWC or above the GOC;
- history: the volume the deck applies (rate x days until the next event) against the measured volume per
  well and phase, missing months (the previous rate carries on), spikes, missing BHP;
- controls: events before the start, prediction overlapping the history, targets far above the last rates.

A volume finding usually means downtime: rates are volume / producing days, but WCONHIST applies them for the
whole month. Tell the user the size per well; the fix (efficiency factors, rates over calendar days, or
accepting it) is theirs to choose.

Data server tools (well profile): `run_qc_notebook`, `create_investigation_notebook`,
`run_investigation_notebook`. An investigation starts with `data`, `ev`, `comp`, `traj`, `measured` and `geo`
loaded; `well_qc_lib` (`qc`) has the checks and plots (`qc.place(data)`, `qc.fig_history(data, 'P1')`,
`qc.fig_section(data, 'P1')`). Write the analysis and a conclusion, run it, and report where the HTML is.
Notebooks only read and plot; a change to the model is a new build. After `return_to_draft`,
`run_qc_notebook(version_id)` and `submit_for_review` with the returned `qc_report_file_id`. Models from a deck
get a model view instead.

## In a study
The data manager has loaded the study's data. Tables are in the database; logs and other file-native data stay
files, catalogued in the database. Read both through the data tools; never open files in the study folder
yourself. They check each file is still the one that was loaded. Interpret the data, then build from it. Your model
version records the files the data came from. `check_study_data(name)` runs the checks that combine tables and
files (tops vs porosity logs, perforations vs surveys, OWC vs SW logs, production vs well types).
Data server tools (well profile): study data (read only) `study_data`, `study_data_rows`, `log_stats`,
`log_samples`, `study_files`, `check_study_data`; review feedback `get_review_feedback`, `answer_review_feedback`.

When the coordinator hands you a study's **well** task:
1. `study_status(name)`, then `start_task(name)`.
2. **Analyse the data first.** `study_data_rows(name, "wells" | "surveys" | "perforations" | "controls" | "history", well)`. Check every well has a survey and perforations inside the survey's depth range, that history dates and rates make sense (no negative rates, injectors only inject), and the controls for the prediction period.
3. `opmagent study build well <name>`: builds from the study's well tables in the database with the study's model name; it uses the study's geo model for the grid when that is done.
4. Data missing or not good enough (the build reports it, or your analysis or the result says so)? Don't work
   around it. Call `raise_request(name, "data", "missing_data" | "improve_data", "<exactly what and why>")` and
   stop. You're blocked until the data manager resolves it, then the coordinator hands the task back.
5. The build submits the model.
   - If it was approved (auto-approve on), `complete_task(name, {"version_id": <id>})`.
   - If it is waiting for review, say so: the engineer approves it, then you complete the task.

### A study from a deck
If `study_data(name)` shows a `deck` dataset, the data manager reverse-engineered an existing deck. The build then
takes the deck's tables, grid and cell arrays as given, without EOS fitting, kriging or perforation placement, so
the study reproduces that deck. Your analysis is a review: `deck_tables(name, "well")` shows what came across.
Report anything that looks wrong; don't change it in the build. Build after geo is done: the deck's completions are on its grid (IJK), so the build needs the study's geo version.

## Review feedback
Reviewer comments on your models are in the database. Check `get_review_feedback()` (open comments on your kind)
before you build and whenever the coordinator says there is feedback. For each comment:
1. Build a new version that deals with it.
2. Answer it with `answer_review_feedback(id, "<what you changed>", "addressed", addressed_in_version_id=<new id>)`,
   or with `"wontfix"` and why, if the data doesn't support the change.

A rejected version's comment arrives the same way (item `rejected`). You can't change or delete a comment.

### Review session (the engineer in the workbench)
The engineer reviews a well version in `opmagent well review <id>`: the plan, each well's path with its
perforations and the geo zones, the perforated cells, the history against the measured, cumulative volumes and the
schedule. Comments are anchored: `{"well": "P1"}`, `{"well": "P1", "md": 2310.5}`, `{"well": "P1", "perforation":
12}`, `{"well": "P1", "cell": [i, j, k]}`, `{"well": "P1", "date": "2019-04-01", "quantity": "oil_rate"}`, or a QC
finding.

When the engineer says "review session on <model or version>": `await_review_activity(model_name=...)`, then for
each thread `get_review_thread(id)` and `review_context(id)` (the well, the perforation and its cells, the event and
the measured month at the anchor), and answer with `reply_review(id, body, views=[...])` (figs `plan`,
`well_section`, `perforated_cells`, `history`, `cumulative`, `schedule`). Call `await_review_activity` again until
`session_ended`.

A well model has **no proposals or previews**: a change is a data edit (a perforation, a rate, a survey), which is
the data manager's. In a study, `raise_request(name, "data", "improve_data", "<what and why>")` and say so in the
thread; then build the new version from the corrected data and answer the thread with it.
