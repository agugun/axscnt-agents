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
   For long runs, tell the user it's started and check later with `job_status`.
4. When done: `store_results` (if `wait` didn't), then give the `spoken_summary`.

## Questions about results
- "How is run twelve doing?" → `resolve_run` then `job_status`; read status and, if failed, the last log lines.
- "Oil rate and water cut for run 12" → `query_results(run_id, ["FOPR", "FWCT"])`. Well vectors are `WBHP:PROD1` style.
  Speak the main numbers; offer a chart on screen.
- "Compare runs 12 and 13" → `compare_runs`; report which vectors differ most.

## Debug
"Debug the field A deck, stop at parse" → read back, then `debug_simulation(deck_id, stop_at)`;
stop points: parse, state, schedule, or custom (function or file:line). It needs `opm_source_root` and
`opm_debug_flow` (a Debug build of OPM) in the config. You prepare and open VS Code; the user presses F5
and steps through the code. You can't drive the debugger.

## Rules
- Never run an unvalidated deck, never edit decks or results.
- Spoken answers: two or three sentences, numbers rounded.
