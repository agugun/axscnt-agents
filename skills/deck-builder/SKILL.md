---
name: deck-builder
description: Build a validated OPM Flow deck from the user's simulation requirement and approved fluid, geo, res and well models. Use when asked to build, make or prepare a simulation deck.
---

# Deck builder

You turn a plain-language simulation requirement into a deck spec, and `opmagent deck build` turns the
spec plus approved models into an OPM deck. You never write deck keywords yourself.

## Tools

- Data server, **deck** profile: `resolve_name`, `list_models` (approved only), `get_model`, `list_decks`, `get_deck`,
  `init_view`.
- Shell: `opmagent deck build --spec <spec.json>`.

## Workflow

1. **Pick the models.** Use the user's words with `resolve_name` ("latest approved field A models").
   Default: the latest approved version of each. If a model they name isn't approved, say so: only
   approved models can be used. Never mix demo (hello) models with real ones.
2. **Write the spec** (JSON):
   ```json
   {"name": "FieldA_WF20", "requirement": "<the user's words>",
    "fluid": "FieldA_Oil", "geo": "FieldA_Base", "res": "FieldA_Res", "well": "FieldA_Wells",
    "start_date": "2026-01-01", "years": 20, "report": "monthly",
    "summary": ["FOPR", "FWCT", "FGOR", "FPR", "WBHP"]}
   ```
   `fluid`/`geo`/`res`/`well` take a model name (latest approved) or a version id; the res version must be defined on
   the geo version. Use `end_date` instead of
   `years` when the user gives one. `report` is monthly, quarterly or yearly. Omit `summary` for the default set.
   Ask for anything you can't infer: the period, report frequency if it matters to them.
3. **Build.** `opmagent deck build --spec spec.json`. It runs the cross-model checks (unit systems,
   PVT pressure range vs initial pressure and BHP limits, region counts, well/geo version match),
   renders the deck and parses it with OPM (EclipseState, Schedule, SummaryConfig).
4. **Report.** `ok: true` → deck path, the model versions used, and that it passed the OPM parser.
   `CROSS_CHECK_FAILED` → explain each problem and which modeler can fix it. A parse failure → give the
   last OPM message and ask whether to rebuild after the fix.
5. **Initial state.** A validated deck is also initialised at time zero (`init_view` in the result: no wells, one
   tiny step). Report the fluids in place (oil, gas, water, average pressure; per FIPNUM region when there are
   several) and any check with `warn`: cells below a contact holding oil or gas point at the res model's contacts or
   SCAL. They can see it in ResInsight: `opmagent results open --deck <deck_id> --init`. `init_view_error` means Flow
   couldn't initialise the deck; give its last lines: the same deck would fail its first run.

## Rules

- Decks are never edited by hand; rebuild from approved models.
- Spoken answers: two or three sentences.

## Studies
**Start a study** when the engineer describes a new simulation question and the coordinator asks you to:
`init_study(name, requirement, years | end_date, start_date, report, summary)`. It creates the study folder
(`inbox/`, `deck/`, `runs/`), `study.json` and the task chain; tell the engineer the inbox path.

**Build the study's deck** when its deck task is ready (all four models done):
1. `study_status(name)`, then `start_task(name)`.
2. `opmagent study build deck <name>`: uses exactly the model versions the modelers completed, and the study's
   period, report frequency and summary vectors (written to `<study>/deck/deck_spec.json`).
3. `ok: true` -> `complete_task(name, {"deck_id": <id>})`. A cross-check failure names the model to fix:
   `raise_request(name, "<fluid|geo|res|well>", "improve_data", "<the problem>")`.

For a study whose data came from a deck (the data manager's reverse process), `opmagent study build deck <name>`
uses the source deck's own dates, report steps, schedule and verbatim keywords rather than the study's period,
so the deck is the same as the source.
