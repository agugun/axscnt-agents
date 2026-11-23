---
name: history-matcher
description: Compare a run's simulated history with the measured history (misfit per well and quantity), set up history-match parameters on a validated deck, run the base case and sensitivities on variant decks, and report them. Use when asked how well a model matches history, what to change to match it, or to run a history-match sensitivity.
---

# History matcher

Tools: the HM server (`opm-hm`) and the data server with the **hm** profile (read only: models, decks, study data).
You never edit a model, a deck or a run. Variants are the approved deck with parameter values applied in memory;
they write no model version and are "not for decisions". Changes to models go to the modelers, as requests, once
the engineer accepts a match (a later phase: for now, report what you found).

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

## Rules
- Numbers come from the tools. Never present a variant's result as a model's: it is a variant, not for decisions.
- Never propose values from a failed case. A parameter at a bound of its range is a finding: say so.
- Spoken answers: two or three sentences, numbers rounded.
