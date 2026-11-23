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
- SCAL from a workbook: `opmagent res template scal.xlsx` gives the engineer the template (sheets Info, RockTypes,
  SWOF, SGOF); `opmagent res build ... --scal scal.xlsx` uses its rock types in place of the spec's `scal`, and
  says what it had to assume. A required gap (no unit system, Sw not increasing, a SATNUM with no rows) stops it.
- Never guess contacts, the datum pressure or SCAL end points: ask. Corey defaults are listed in the assumptions
  when a parameter is left out; tell the user which ones.

## Workflow
1. Find the geo version the model is for (`list_models(kind="geo")`; in a study, the geo task's result).
2. Write the spec from the user's words and data, read it back briefly.
3. `opmagent res build --spec res.json --geo-version <id> --name <Name>`
4. Report: SATNUM / EQLNUM counts, datum and contacts, the QC report path, and the assumptions. It is then
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

## Rules
- Never invent SCAL, contacts or pressures; never edit table numbers by hand; never approve.
- Units follow the geo version (METRIC: bar, m, 1/bar).
