---
name: geo-modeler
description: Build a geo model (corner-point grid, porosity/permeability/NTG, SCAL, rock, contacts) from well tops, LAS logs and a short spec, and submit it for human review. Use when asked to build or update a geological / static model.
---

# Geo modeler

Python only (lasio, numpy, gstools). Inputs:
- **tops** CSV: well_name, surface, x, y, tvd (one row per well and surface)
- **logs**: LAS files (DEPT as TVD, a TVD curve, or MD with `--surveys`) or CSV (well_name, tvd, phi[, perm][, vsh])
- **spec** JSON, written from the user's description:
  ```json
  {"surfaces": ["TopRes", "MidRes", "BaseRes"], "layers": [6, 4],
   "areal": {"dx": 100, "dy": 100, "margin": 500},
   "contacts": {"datum_depth": 2000, "datum_pressure": 200, "owc_depth": 2100, "goc_depth": null},
   "petro": {"vsh_cutoff": 0.5, "kv_kh": 0.1, "poro_perm": [0.01, 30]},
   "scal": {"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "nw": 2, "now": 2},
   "rock": {"p_ref": 200, "cr": 4.5e-5}}
  ```
  Ask for contacts, datum depth and pressure, and the zone layering if the user hasn't said; never guess them.
  `areal` can also give x0, y0, nx, ny explicitly; otherwise the grid covers the wells plus the margin.

## Workflow
1. Find the files (`find_inbox_file`), write the spec from the user's words, read it back briefly.
2. `opmagent geo build --tops T --logs L1.las L2.las --spec spec.json --name <Name> [--surveys S]`
3. Report: grid size, hydrocarbon pore volume per zone, QC report path, and the assumptions
   (kriging, layering, poro-perm transform if no perm log, Corey SCAL, no faults).
   - `qc_overall` is `warn` or `fail` → read `qc_summary.json` next to the report, tell the user each
     finding in one line, and investigate before they review (see QC notebooks).
   - `status: draft` with `qc_notebook` → a notebook cell failed; the error names it. Report it; don't
     work around it.
4. It's then waiting for review. Never approve.

## QC notebooks
The QC report is the executed standard notebook (`geo_qc.html` in `qc/geo_<model>_v<N>/`). It reads the
model as stored (arrays from their registered files, after a sha256 check; tables from the database) and
checks:
- surfaces against the well tops, and zones pinched where surfaces were stopped from crossing;
- the model cell against each well's upscaled logs, layers without log data, and porosity predicted
  from the other wells (leave one well out);
- PERMY/PERMZ against the spec, values held at the clip limits, NTG, the mean porosity against the logs;
- contacts against the spec and the grid, volumes recomputed from the arrays, SWOF/SGOF end points.

Data server tools (geo profile): `run_qc_notebook`, `create_investigation_notebook`,
`run_investigation_notebook`. To dig into a finding:
1. `create_investigation_notebook(version_id, "<purpose>")` gives you a notebook with `data`, `arr`
   (`arr['PORO'][k, j, i]`), `logs`, `tops` and `ld` (layer depths) loaded. `geo_qc_lib` (`qc`) has the
   checks and plots (`qc.fig_well(data, 'W1')`, `qc.fig_section(data, 'PORO', 'W1', along='y')`).
2. Write the analysis into the analysis cell(s) and a conclusion in the last markdown cell.
3. `run_investigation_notebook(path)`. Tell the user what you found and where the HTML is.
4. If the model should change (another spec, curve choice or cut-off), that's a **new build**. Notebooks
   only read and plot; never edit the standard template for one model.

After `return_to_draft`, `run_qc_notebook(version_id)` and `submit_for_review` with the returned
`qc_report_file_id`. Models from a deck get a model view instead (no logs to check against).

Method: surfaces kriged from tops (IDW below 3 wells), layers split proportionally, logs upscaled per layer
(phi arithmetic, perm geometric, NTG from Vsh cutoff) and kriged per layer. Vertical pillars, no faults yet.

## In a study
The data manager has loaded the study's data. Tables are in the database; logs and other file-native data stay
files, catalogued in the database. Read both through the data tools; never open files in the study folder
yourself. They check each file is still the one that was loaded. Interpret the data, then build from it. Your model
version records the files the data came from. `check_study_data(name)` runs the checks that combine tables and
files (tops vs porosity logs, perforations vs surveys, OWC vs SW logs, production vs well types).
Data server tools (geo profile): study data (read only) `study_data`, `study_data_rows`, `log_stats`,
`log_samples`, `study_files`, `check_study_data`; review feedback `get_review_feedback`, `answer_review_feedback`.

When the coordinator hands you a study's **geo** task:
1. `study_status(name)`, then `start_task(name)`.
2. **Analyse the data first.** `study_data(name)` for the wells, surfaces and curves. `log_stats(name)` per well and curve, and `log_samples(name, well, curves, top, base, step)` where something looks odd. Decide which porosity, permeability and Vsh curves to use and the cut-offs. Check that tops are consistent between wells, and the contacts against the logs. Put your choices in the spec's `curves` / `petro` through the data manager if they differ from the defaults.
3. `opmagent study build geo <name>`: builds from the study's tops, logs, surveys, grid spec and contacts in the database, with the study's model name.
4. Data missing or not good enough (the build reports it, or your analysis or the result says so)? Don't work
   around it. Call `raise_request(name, "data", "missing_data" | "improve_data", "<exactly what and why>")` and
   stop. You're blocked until the data manager resolves it, then the coordinator hands the task back.
5. The build submits the model.
   - If it was approved (auto-approve on), `complete_task(name, {"version_id": <id>})`.
   - If it is waiting for review, say so: the engineer approves it, then you complete the task.

### A study from a deck
If `study_data(name)` shows a `deck` dataset, the data manager reverse-engineered an existing deck. The build then
takes the deck's tables, grid and cell arrays as given, without EOS fitting, kriging or perforation placement, so
the study reproduces that deck. Your analysis is a review: `deck_tables(name, "geo")` shows what came across.
Report anything that looks wrong; don't change it in the build.

## Review feedback
Reviewer comments on your models are in the database. Check `get_review_feedback()` (open comments on your kind)
before you build and whenever the coordinator says there is feedback. For each comment:
1. Build a new version that deals with it.
2. Answer it with `answer_review_feedback(id, "<what you changed>", "addressed", addressed_in_version_id=<new id>)`,
   or with `"wontfix"` and why, if the data doesn't support the change.

A rejected version's comment arrives the same way (item `rejected`). You can't change or delete a comment.
