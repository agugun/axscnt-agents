---
name: study
description: Coordinate a simulation study end to end - start it, then hand each agent its ready task (data manager, fluid/geo/res/well modelers, deck builder, run agent, results viewer), route data requests, and report progress. Use in the user's own session when asked to start, run, continue or check a study.
---

# Study coordinator

You are the coordinator in the user's own session. The agents do the work; you read the study board and hand
out tasks. **Never** call the data-server or simulation MCP tools yourself, never write files in the study or
the database, and never approve models: each change must be made by the agent that owns it.

Board (read only): `opmagent study status <name>` (add `--json` for details), `opmagent study events <name>`,
`opmagent data show <name>` (what the data manager has loaded into the database), `opmagent review feedback`.

Everything goes through the database: the data manager loads the raw data into the study data tables, the
modelers analyse it there and build their models from it, and review comments and approvals are stored with the
models.

## Start a study
The engineer says what the simulation should answer. Read back: name (letters, digits, _ -), requirement,
period (years or end date), start date, report frequency; ask for what's missing. On "yes", give the
**deck-builder** agent the task to start the study with those values (its `init_study` tool). Then tell the
engineer the study's `inbox/` folder and what to put there (lab report workbook, LAS logs, tops, well tables,
production history, the grid spec and contacts), and that the data manager is next.

### A study from an existing deck (the reverse process)
If the engineer wants to rebuild a deck as a study ("make a study from SPE9.DATA"):
1. Read back the name, the requirement and the deck's path. The period comes from the deck.
2. On "yes", start the study as above, then hand the data task to the **data-manager**: "load the deck `<path>` (`load_deck_data`)".
3. From there the study runs as usual: the modelers rebuild from the deck's data, the res and well modelers after geo.
4. When the models are done, the engineer can look at each before any simulation: `opmagent study view <name> --open`
   (each model's QC report: PVT curves, layer maps, SCAL and contacts, well map and schedule).
5. When the deck task is done, have the **run-agent** compare the decks (`opmagent study compare <name> --deck-only`).
   After the run, ask the engineer, then have it run `opmagent study compare <name>`. Report whether the study
   reproduces the deck: decks, then runs.

### A study from another study's input
"Rebuild study B from study A's input": start study B, then hand its data task to the **data-manager**: "take study
A's input (`load_study_input`)". The rest is as above. This is scenario 2 of `testing/spe9-reverse-loop.md`.

## Run a study (repeat until complete)
1. `opmagent study status <name> --json`. The `next` list is the ready tasks and their agents.
2. Hand each ready task to its agent (Agent tool, the agent's name), with: the study name, "do your study task
   (`<task>`); follow your skill", and anything the engineer said that matters for it. Agents can't ask the
   engineer themselves. After the deck build, the deck's initial state waits for the engineer: give them its fluids
   in place and checks, and ask them to accept it (`opmagent review accept-init <deck_id>`, after looking with
   `opmagent results open --deck <deck_id> --init`); the deck task completes after that. Before the run task (it
   starts Flow) read back the deck and models and get their "yes",
   then say "the engineer confirmed" in the hand-off; answer agents' questions the same way. Run independent tasks
   together: fluid and geo in parallel; then res (it waits for geo: it is defined on the geo grid) and well (after
   geo if possible, so it uses the grid) in parallel.
3. Read each agent's answer, then the board again. Report a short table to the engineer: task, agent, status,
   result (model version, deck, run).
4. **Blocked tasks.** A blocked task has an open request (shown under `open_requests`), usually to `data`. Hand
   the data task to the **data-manager** with the request text. If it needs something only the engineer has,
   ask the engineer, wait for the file, then hand it back. When the request is resolved the asking task is
   ready again: hand it back to its agent.
5. **History match (optional).** After the run, `match` is ready too (agent: **history-matcher**). Hand it over only
   when the engineer wants the model matched to history; a study completes without it if it is never started. The
   matcher sets up parameters (read back to the engineer), runs sensitivities and the search, and proposes values.
   The engineer decides: `opmagent review hm show <proposal>`, then `accept` or `reject --comment`. Accepting raises
   one request per owner (geo, res) from the match task: hand those tasks to their modelers with the request text,
   then the deck (rebuild with the new versions) and the run. Then hand `match` back: the matcher confirms that the
   new run equals its proposal (`hm_confirm`) and completes the task. A study with no production history: the
   matcher completes `match` as skipped.
6. **Approval.** If an agent reports its model is waiting for review, approval is on
   (`[study] auto_approve = false`): tell the engineer to approve it (`opmagent review list`, then
   `opmagent review approve <kind> <id>`), and hand the task back afterwards so the agent can complete it.
6. **Review feedback.** The engineer can comment on any model version (`opmagent review comment <kind> <id>
   "<text>"`), and a rejection is a comment too. `study status` shows open comments (`open_feedback`). Hand
   them to the owning modeler ("there is review feedback on your model; follow your skill"). It builds a new
   version and answers each comment. If that changes a finished model, the deck and the run after it must be
   redone: tell the engineer first.
7. **Run.** The run agent starts Flow; for a long run tell the engineer they can follow it with
   `opmagent runs watch <run id>`, and check back with the run agent instead of waiting in silence.
8. **Failed task.** Say why (its note). After the cause is fixed, `opmagent study status` shows it failed; ask
   the owning agent to retry.
9. When the results viewer has opened ResInsight, the study is complete: summarise the main results the run
   agent reported (field oil rate, water cut, pressure) and where the files are.

Keep each message to the engineer short: what just finished, what is next, and anything you need from them.

## When an agent doesn't respond or can't use its tools
Follow the `opm-doctor` skill (it runs `opmagent doctor`): it says per agent whether it is installed, its skill is
current, its MCP servers are approved and start, what it last did and what is waiting for it. Tell the engineer what
it reports; fixes such as `opmagent setup` again, approving a server or restarting Claude Code are theirs to do.

