---
name: study
description: Coordinate a simulation study end to end - start it, then hand each agent its ready task (data manager, fluid/geo/well modelers, deck builder, run agent, results viewer), route data requests, and report progress. Use in the user's own session when asked to start, run, continue or check a study.
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

## Run a study (repeat until complete)
1. `opmagent study status <name> --json`. The `next` list is the ready tasks and their agents.
2. Hand each ready task to its agent (Agent tool, the agent's name), with: the study name, "do your study task
   (`<task>`); follow your skill", and anything the engineer said that matters for it. Agents can't ask the
   engineer themselves: before the run task (it starts Flow) read back the deck and models and get their "yes",
   then say "the engineer confirmed" in the hand-off; answer agents' questions the same way. Run independent tasks
   together: fluid and geo in parallel; well after geo is done if possible (it then uses the geo grid).
3. Read each agent's answer, then the board again. Report a short table to the engineer: task, agent, status,
   result (model version, deck, run).
4. **Blocked tasks.** A blocked task has an open request (shown under `open_requests`), usually to `data`. Hand
   the data task to the **data-manager** with the request text. If it needs something only the engineer has,
   ask the engineer, wait for the file, then hand it back. When the request is resolved the asking task is
   ready again: hand it back to its agent.
5. **Approval.** If an agent reports its model is waiting for review, approval is on
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
