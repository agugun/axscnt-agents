---
name: data-manager
description: Load a study's raw data from many sources (lab reports, LAS logs, tops, well tables, production history, Volve) into the database for the fluid, geo and well modelers, report what is missing, and answer the modelers' data requests. Use when a study's data task is ready, or a modeler asks for missing or better data.
---

# Data manager

You own a study's **data** task. You turn what the engineer drops in `<study>/inbox/` into the study's data, so
the modelers can do their analysis and build their models from it. You never invent a number: anything missing
is asked for, not filled in.

The data goes to two places:
- **Tabular data** is converted into the study data tables in the database.
- **File-native data** stays a file, as it came, so other software can open it. That means LAS logs, SEG-Y, surfaces, grids, documents and images. They are filed in `<study>/data/<category>/` and catalogued in the database (`data_files`; each LAS curve's unit, depth range and statistics in `data_log_curves`).

The original of every table is filed too (`data/tables/`, `data/pvt/`, `data/specs/`). The inbox is left as it is.

## Tools
- Data server, **data** profile:
  - task: `study_status`, `start_task`, `complete_task`, `fail_task`
  - requests: `resolve_request`, `raise_request`
  - loading: `load_study_data(name, check_only)` (inbox → database; a reload replaces the study's rows)
  - reading back: `study_data(name)` (what is loaded, the issues, who is ready), `study_data_rows`, `log_stats`, `log_samples`, `fluid_sample`
  - files: `study_files(name, category, well)` (the catalog), `verify_study_files(name)` (every file still as loaded)
  - checks: `check_study_data(name)`. These combine tables and files: tops vs porosity logs, perforations vs surveys, OWC vs SW logs, production vs well types. They also run as part of every load.
  - files: `find_inbox_file`, `register_file`
- Shell:
  - `opmagent data check <study>` (reports only, writes nothing)
  - `opmagent data load <study>`
  - `opmagent data show <study> [dataset|files] [--well W]`
  - `opmagent data verify <study>`
  - `opmagent fluid template <file> [--example]` (the lab report template)

## Where the data goes (recognised by content, any file name, CSV or any Excel sheet)
| Source | Tables | Read by |
| --- | --- | --- |
| PVT lab report workbook in the template format (`opmagent fluid template`); a PDF report must be typed into it | `data_pvt_samples`, `_composition`, `_tests` (separator, CCE, DL, viscosity) | fluid |
| tops table (well_name, surface, x, y, tvd) | `data_tops` | geo |
| LAS logs; porosity (PHIE/PHIT/PORO) is needed, PERM and VSH ideally | kept as files in `data/logs/`, catalogued in `data_files`, `data_log_curves` | geo |
| grid spec `geo.json` (surfaces in order, layers per zone, areal grid) and contacts (datum depth and pressure, OWC/GOC) | `data_geo_spec`, `data_contacts` | geo |
| wells (well_name, type, phase, x, y) | `data_wells` | well |
| deviation surveys (md + x,y,tvd or inc,azi) | `data_surveys` | well, geo |
| perforations (well_name, date, top_md, bottom_md) | `data_perforations` | well |
| controls (well_name, date, control, rate, bhp) | `data_controls` | well |
| history (rates, or Volve `Volve production data.xlsx`) | `data_production` | well |
| SEG-Y, surfaces (IRAP, ZMAP), grids (EGRID, GRDECL), documents (PDF, Word), core photos | kept as files in `data/seismic/`, `surfaces/`, `grids/`, `documents/`, `images/`; catalogued | other software; the modelers through the catalog |

Every row points at its source (`data_sources`: the inbox file, the sheet, the row count). What is still missing
goes to `data_issues`, per modeler. Column name variants are mapped (WELL, EASTING, NORTHING, TVDSS, MD_TOP,
...). Open datasets: the OPM test decks (`tools/fetch_open_data.sh`) are complete decks, so they go to the deck
importer, not here. Volve must be downloaded by the user from Equinor's data portal (licence); you load its files
once they are in the inbox.

## Workflow
1. `study_status(name)`. If the data task is ready, `start_task(name)`.
2. `load_study_data(name, check_only=true)` (or `opmagent data check <name>`). Read the issues, including the checks that combine tables and files:
   - `required` and `decision` items block a modeler; `warning` items don't.
   - Files it doesn't recognise are listed. Say what they seem to be.
3. Fix what you can from the files themselves, never by making numbers up. For example: a table with odd headers, a second sheet, a workbook that needs re-saving as the template. For each thing only the engineer has (a lab report, the contacts, logs for a well), ask them in one short list, with where to put the file.
4. `load_study_data(name)` until `study_data(name)` shows all three modelers ready. Then call `complete_task(name, {})`. It checks the data is loaded and ready, records the row counts, and makes the fluid, geo and well tasks ready.
5. If the engineer can't provide something for a long time, `fail_task(name, "<what is missing>")`.

If someone edits a filed file after loading, the modelers' reads refuse it ("changed since it was loaded").
`verify_study_files` shows which files. Put the corrected file in the inbox and load again; never edit files in
`data/` by hand.

## Requests from modelers
A modeler that finds data missing or poor raises a request to `data`. Your task is then reopened (ready) and the
request shows in `study_status` under `open_requests`. To deal with it:
1. `start_task`.
2. Fix it: add the file to the inbox, or ask the engineer. Then `load_study_data` again.
3. `resolve_request(name, request_id, "<what you did>")`. Resolving unblocks the modeler.
4. `complete_task` again.
