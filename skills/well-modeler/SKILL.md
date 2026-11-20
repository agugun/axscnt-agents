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
