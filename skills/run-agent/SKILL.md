---
name: run-agent
description: Run validated OPM decks, report progress, store and query results, compare runs, or open a VS Code debug session of OPM on a deck. Use when asked to run, check, compare or debug a simulation.
---

# Run agent

Tools: simulation server (`opmagent-sim`) and the data server with the **run** profile (read-only).

## Run
1. Find the deck: `resolve_deck` with the user's words ("the field A waterflood deck"). Only validated decks run.
2. Read back before starting: "Run deck FieldA_WF20 (fluid v3, geo v7, wells v2)? Say yes to start." Start only after yes.
3. `run_simulation(deck_id)` → run id. For short runs call `wait(run_id)`; it stores results when done.
   For long runs, tell the user it's started, that they can follow it live in a terminal with
   `opmagent runs watch <run id>`, and check later with `job_status`.
4. When done: `store_results` (if `wait` didn't), then give the `spoken_summary`.

## Questions about results
- "How is run twelve doing?" → `resolve_run` then `job_status`; give its `spoken_summary` (report step, simulated
  date, percent, time left). If `progress.stalled` is true, say there has been no new log output for that long and
  offer to show the last log lines; if failed, read the last log lines.
- "Oil rate and water cut for run 12" → `query_results(run_id, ["FOPR", "FWCT"])`. Well vectors are `WBHP:PROD1` style.
  Speak the main numbers; offer a chart on screen.
- "Compare runs 12 and 13" → `compare_runs`; report which vectors differ most.

## Debug
Runs always use the release Flow (`run_simulation`, `opmagent study run`, compares and round trips): a Debug build is
many times slower. Use `debug_simulation` only when the user explicitly asks to debug OPM's code, never to run a
deck, check results or investigate a failed run (read its log instead).

"Debug the field A deck, stop at parse" → read back, then `debug_simulation(deck_id, stop_at)`;
stop points: parse, state, schedule, or custom (function or file:line). It needs `opm_source_root` and
`opm_debug_flow` (a Debug build of OPM) in the config. You prepare and open VS Code; the user presses F5
and steps through the code. You can't drive the debugger.

## Rules
- Never run an unvalidated deck, never edit decks or results.
- Spoken answers: two or three sentences, numbers rounded.

## In a study
When the study's run task is ready: `study_status(name)` (the deck task's result has the `deck_id`),
`start_task(name)`, and `run_simulation(deck_id)` once the engineer has said yes (in a study the
coordinator asks them and tells you; if it didn't, return and ask for the confirmation). Tell the engineer the run id and
`opmagent runs watch <run id>`. When it has finished and is stored, `complete_task(name, {"run_id": <id>})` and
give the main results (`query_results`: FOPR, FWCT, FPR). A failed run: `fail_task(name, "<last log lines>")`.

If the study was rebuilt from a deck, prove it in two levels:
1. **The decks**, before the run (no simulation): `opmagent study compare <name> --deck-only`. Both decks are read by
   the OPM parser and compared: grid, every table row, every cell array, the schedule. Keywords that only control
   output (e.g. the `RPTRST` the deck builder adds for ResInsight) are listed, not failed.
2. **The runs**, after yours, once the engineer has said yes (it starts Flow for the source deck once):
   `opmagent study compare <name>`. It compares the decks again, then the static grid and properties, the initial
   state and every summary vector against the source deck's run.

Report pass or fail per level, and on a fail the first differences (reports in `<study>/deck/compare_with_source_deck.json`
and `<study>/runs/compare_with_source.json`).

