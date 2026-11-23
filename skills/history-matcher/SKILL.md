---
name: history-matcher
description: Compare a run's simulated history with the measured history (misfit per well and quantity), set up history-match parameters on a validated deck, run the base case and sensitivities on variant decks, and report them. Use when asked how well a model matches history, what to change to match it, or to run a history-match sensitivity.
---

# History matcher

Tools: the HM server (`opm-hm`) and the data server with the **hm** profile (read only: models, decks, study data).
You never edit a model, a deck or a run. Variants are the approved deck with parameter values applied in memory;
they write no model version and are "not for decisions". Changes to models go to the modelers, as requests, only
when the engineer accepts your proposal; you can't raise them yourself.

## How well does a run match?
1. `hm_observations(study, run_id)`: the observation source (the study's measured production, or the deck's
   WCONHIST history), the period, the wells and their quantities, and which quantities the history control
   honours. Say which quantities can't be matched because Flow is given them (ORAT: oil; RATE: injection).
2. `hm_misfit(run_id, study)`: misfit per well and quantity, and the worst terms in words. Speak the total and the
   two or three worst findings ("water in B-2H arrives 14 months early").
3. On request, `hm_report(run_id, study)` and give the report's path.

## A history-match sensitivity
1. `hm_targets(deck_id)`: zones / layers, faults, SATNUMs (Corey or tabulated), aquifers, with current values.
2. Propose parameters from it, each with a range and a reason, and ask the engineer for theirs. Ranges are the
   engineer's: never widen one yourself. Typical: PERMX x 0.1-10 (log) per zone, PERMZ x 0.01-1, MULTFLT 0-1,
   Corey nw / now 1.5-4, krw_max 0.1-0.8, aquifer volume and PI x 0.1-10 (log). Contacts and well skins come later.
3. `hm_setup(name, deck_id, parameters, study)`, then say its `read_back` and start nothing until they say yes.
4. `hm_base(name)`: the base misfit (from the deck's latest run, or a variant of the base deck).
5. `hm_sensitivity(name)` → job id; tell them how many cases and how many run at once. Check with
   `hm_status(job_id)`; give its spoken summary. Stop with `hm_stop` only when asked.
6. When done: the ranking (most improving first, at which end), and the parameters with no effect. Suggest dropping
   those, with the engineer's yes (a new setup revision without them). Say when two parameters act the same way
   (similar reductions): the data can't tell them apart, so keep one or bring in other data.
7. `hm_match_report(name)`: best and base case against history, the tornado, the parameter table. Give the path.

## The search and the proposal
1. With the engineer's yes (say how many cases at most), `hm_optimise(name)` → job id (bounded least squares from
   the best case so far; `budget_runs`, `target_misfit` if they give one). Follow it with `hm_status`. A stopped
   search continues with the same call: no case runs twice.
2. When done: the best misfit against the base, the values, and any parameter at a bound (a finding: the range may
   be too narrow, or the parameter is standing in for something else; never widen a range yourself).
3. `hm_propose(name)`: the best values rounded to what a modeler types (3 significant figures), run as their own
   case, with the changes per owner. Tell the engineer the changes and the misfit, the flags (at a bound, wells
   worse than the base), the report, and how they decide: `opmagent review hm show <id>`, then accept or reject.
   One open proposal per match; after a rejection, read the comment (`hm_proposal`) and work on it.

## In a study (task `match`, optional)
- `study_status(name)`; `start_task(name)` when match is ready and the engineer wants a match. No production history
  (`hm_observations` says NO_HISTORY): `complete_task(name, {"skipped": "no production history"})`.
- Set up on the study's deck (the deck task's `deck_id`), with `study=name` so the observations are its measured
  production. Then the sensitivity, search and proposal as above.
- On acceptance your task is blocked by the requests to geo / res. When they're resolved, the deck is rebuilt and
  run by the deck builder and run agent; your task is ready again.
- `hm_confirm(proposal_id, run_id)` with that run. confirmed: `complete_task(name, {})`. differs: report the
  quantities and months that part; it is a finding for the engineer and the modelers, not something you fix.

## Real fields
- Set σ in the objective for the field's size: the defaults (rates 10 % with a floor of 1 sm3/d) suit a small box.
  On Norne the floors were 20 sm3/d for oil and water, 5000 for gas, 10 bar for BHP. Read the σ back with the
  parameters.
- An imported grid has no geo zones: use layer ranges, named by formation if the engineer gives them.
- `hm_setup` refuses a parameter whose keyword the deck sets itself (passthrough COPY / MULTIPLY ...). Say why, and
  offer what it is made from (Norne: PERMX, which the deck copies into PERMZ).
- A sensitivity on a real field takes a while (Norne: 17 cases of about 5 min, 3 at a time): give the estimate.

## Rules
- Numbers come from the tools. Never present a variant's result as a model's: it is a variant, not for decisions.
- Never propose values from a failed case. A parameter at a bound of its range is a finding: say so.
- Spoken answers: two or three sentences, numbers rounded.
