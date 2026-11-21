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
   [--controls C] [--groups G] --name <Name> [--start-date YYYY-MM-DD]`
3. Read the result: `qc_ready` → tell the user it's waiting for review (wells, perforations, history months,
   QC report path). Validator errors → explain each (e.g. "producer needs a BHP limit in prediction") and
   ask for the fix; don't invent numbers.
4. History is aggregated to monthly average rates (volume / producing days); say so if asked.
5. Perforations are stored by MD; the deck builder places them in grid cells (regular grids and grids from
   the geo modeler).

Never approve; never edit rates by hand.

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
