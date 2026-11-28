---
name: res-modeler
description: Build a reservoir-engineering (res) model on a geo grid: relative permeability and capillary pressure (SWOF/SGOF per SATNUM), rock compressibility, contacts and initial pressure (EQUIL per EQLNUM), and region arrays (SATNUM, EQLNUM, FIPNUM, ROCKNUM), then submit it for human review. Use when asked for SCAL, relative permeability, initial conditions, contacts, regions or the dynamic part of a reservoir model.
---

# Res modeler

The res model is the dynamic half of the reservoir model, defined on one **geo version's grid**. The geo model
holds the static half (grid, porosity, permeability, NTG, faults); you hold:
- **SCAL**: SWOF and SGOF per SATNUM, from measured tables or Corey parameters (say which, and the source);
- **rock**: compressibility and reference pressure per region;
- **contacts and initial pressure**: datum depth and pressure, OWC, GOC per EQLNUM (they feed EQUIL);
- **region arrays**: SATNUM, EQLNUM, FIPNUM, ROCKNUM, by zone of the geo grid or by layer range.

A deck needs four approved models: fluid, geo, res and well. Your res version names its geo version, and the deck
builder uses them together.

## Spec
```json
{"scal": {"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "sgc": 0.05, "nw": 2, "now": 2, "ng": 2, "nog": 2,
          "krw_max": 0.4, "krg_max": 0.8, "source": "analogue field core, report X"},
 "rock": {"p_ref": 200, "cr": 4.5e-5},
 "contacts": {"datum_depth": 2000, "datum_pressure": 200, "owc_depth": 2100, "goc_depth": null},
 "regions": {"SATNUM": {"by": "zone", "values": {"Upper": 1, "Lower": 2}}}}
```
- Several rock types: `"scal": [{"satnum": 1, ...Corey...}, {"satnum": 2, "swof": [...], "sgof": [...]}]` with
  measured rows `{sw, krw, krow, pcow}` / `{sg, krg, krog, pcog}`. Each SATNUM used by the regions needs a table.
- Several equilibration regions: `"contacts": [{"eqlnum": 1, ...}, {"eqlnum": 2, ...}]` with an EQLNUM region rule.
- Region rules: `{"by": "zone", "values": {zone: n}}` (geo modeler grids), `{"by": "layer", "values": {"1-3": 1}}`,
  or `{"value": 1}`.
- Analytical aquifers: `"aquifers": [...]`, each Fetkovich `{"type": "fetkovich", "ct", "water_volume",
  "productivity_index"}` or Carter-Tracy `{"type": "carter_tracy", "ct", "permeability", "porosity",
  "inner_radius", "thickness", "angle"}`, with `"connections": [{"face": "I-"|"I+"|"J-"|"J+"|"K-"|"K+",
  "i"?, "j"?, "layers"? | "zone"?, "influx_mult"?}]` (a face alone means that whole edge of the grid). Optional:
  `datum_depth` (default the OWC), `init_pressure` (default equilibrated), `pvtw_table`, `source`. Units are the
  deck's (METRIC: sm3, sm3/day/bar, 1/bar, mD, m). Never invent an aquifer's size or productivity: ask, and say
  where the numbers come from (material balance, analogue, history match).
- SCAL from a workbook: `opmagent res template scal.xlsx` gives the engineer the template (sheets Info, RockTypes,
  SWOF, SGOF); `opmagent res build ... --scal scal.xlsx` uses its rock types in place of the spec's `scal`, and
  says what it had to assume. A required gap (no unit system, Sw not increasing, a SATNUM with no rows) stops it.
- Never guess contacts, the datum pressure or SCAL end points: ask. Corey defaults are listed in the assumptions
  when a parameter is left out; tell the user which ones.

## Workflow
1. Find the geo version the model is for (`list_models(kind="geo")`; in a study, the geo task's result).
2. Write the spec from the user's words and data, read it back briefly.
3. `opmagent res build --spec res.json --geo-version <id> --name <Name>`
4. Report: SATNUM / EQLNUM counts, datum and contacts, the QC notebook's fail and warn findings, the QC report
   path, and the assumptions. To dig into a finding: `create_investigation_notebook(version_id, purpose)`, add
   cells, `run_investigation_notebook(path)` (read only; `qc.end_points(data)`, `qc.region_volumes(data, "FIPNUM")`,
   `data['grid']` is the static view). After return_to_draft, `run_qc_notebook(version_id)`. It is then
   waiting for review. Never approve.

## In a study
The res task is ready when geo is done. `study_status(name)`, then `start_task(name)`. The contacts come from the
data manager (`study_data(name)` → `contacts`); SCAL from the study's SCAL workbook (`study_data(name)` →
`scal`, loaded by the data manager), else the geo spec's `scal`; rock and regions from the engineer's geo spec
entries (`rock`, `regions`) if given. No workbook and no `scal`: Corey defaults, which you list for the engineer. `opmagent study build res <name>` builds on the study's geo version with the
study's model name. Missing contacts or a datum pressure? `raise_request(name, "data", "missing_data", "...")` and
stop. When the version is approved (auto-approve on, or the engineer approved it), `complete_task(name,
{"version_id": <id>})`.

### A study from a deck
If `study_data(name)` shows a `deck` dataset, the build takes the deck's SWOF/SGOF, ROCK, EQUIL contacts and
region arrays as given, on the study's geo version. Review what came across with `deck_tables(name, "res")`;
report anything that looks wrong, don't change it.

## Review feedback
Check `get_review_feedback()` before you build. For each comment build a new version that deals with it and answer
with `answer_review_feedback(id, "<what changed>", "addressed", addressed_in_version_id=<new id>)`, or `"wontfix"`
and why.

### Review session (the engineer in the workbench)
The engineer reviews a res version in the workbench (`opmagent review open res <id>`, or `opmagent res review <id>`):
the QC report first, then comments with you, then the decision. It shows relative permeability and capillary pressure per
SATNUM, region maps, contacts per EQLNUM, a section of cells at their depths with the contacts, aquifers. Their
comments are anchored: `{"satnum": 2, "curve": "krw", "s": 0.45}`, `{"eqlnum": 1, "contact": "OWC"}`,
`{"cell": [i, j, k], "kw": "SATNUM"}` (1-based), `{"aquifer": 1}`, or a QC finding.

When the engineer says "review session on <model or version>":
1. `await_review_activity(model_name=...)` (or `version_id`): `waiting_on_you` with the threads, `session_ended`
   (stop), or `timeout` (call it again).
2. For each thread: `get_review_thread(id)` and `review_context(id)` (the end points, pore volumes per contact
   interval or the cell's values at the anchor, and what the view shows). Then:
   - **a question:** `reply_review(id, body, views=[...])` with numbers and views (`render_review_view` first;
     figs `kr`, `pc`, `regions`, `contacts`, `contact_section`, `aquifers`).
   - **a change:** `preview_res_change(base_version_id, spec_patch)` (well under a second: pore volume in the gas
     cap, oil column and water per EQLNUM, end points per SATNUM, QC findings), then
     `propose_res_change(base_version_id, [ids], spec_patch, rationale, expected, preview_id=...)`. A list in the
     spec (several contacts or rock types) is replaced whole by the patch. Never build before it is accepted.
   - **an accepted proposal** (yours, or the engineer's own, already accepted): `opmagent res build --from-proposal
     <id>` (in a study: `opmagent study build res <study> --from-proposal <id>`), then `answer_review_feedback` on
     its threads with the new version; `review_diff(new id)` says what changed.
3. Call `await_review_activity` again until `session_ended`. Never accept, approve or start the workbench.

## Rules
- Never invent SCAL, contacts or pressures; never edit table numbers by hand; never approve.
- Units follow the geo version (METRIC: bar, m, 1/bar).
