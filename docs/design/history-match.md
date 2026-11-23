# History matching

Status: proposal, for review before implementation. Branch: `docs/history-match`.

## Summary

A **history matcher** agent compares a study's simulated history with the measured history, well by well, and
searches for parameter changes that bring them together. It never edits a model. It hands the parameters it found
to the modelers who own them, as requests. They build new versions, you review and approve them as now, and the
matched deck goes through the deck build, the initialisation gate and a full Flow run. The run's report confirms
the match.

```
measured history (data manager: data_production) ──┐
model set, approved (fluid, geo, res, well with HISTORY events) ──► deck ──► Flow run
                                                                         │
                         history matcher: misfit per well and quantity ◄─┘
                                │  sensitivities (parallel Flow variants), then optimisation
                                ▼
          match proposal: parameters, best misfit, plots (HM report) ──► you review it
                                │  accept
                                ▼
   requests to the modelers: "geo: PERMX x 0.6 in zone Lower", "res: aquifer 1 PI 80, Corey nw 2.6"
                                ▼
        new versions ──► your approval ──► deck ──► init gate ──► Flow run ──► HM report: match confirmed
```

Ownership stays as it is: the matcher proposes and the modelers build. Every change is an ordinary reviewed
version, so a matched model is as traceable as any other.

## What already exists

- **Measured history:** the data manager loads `data_production` (monthly oil, water, gas, water and gas
  injection, BHP, THP, hours per well, including Volve). The well modeler turns it into `HISTORY` events, which the
  deck builder renders as `WCONHIST` / `WCONINJH`.
- **Simulated history:** Flow writes per-well vectors (`WOPR`, `WWPR`, `WGPR`, `WBHP`, `WWIR` ...) and, for
  wells under history control, the observed ones it was given (`WOPRH`, `WWPRH`, `WGPRH`, `WBHPH`, `WWIRH`).
  `store_results` keeps them in `results`.
- **Comparison:** `compare_runs` and the deck comparison's dynamic level compare two runs vector by vector.
- **Real test case:** Norne (opm-tests) has its production history in `WCONHIST`, 1997-2006, and is a public
  history-matching benchmark. SPE1 and SPE9 have no measured history.

## Design

### 1. Observations and misfit

**Observations** come from `data_production` (the measured values, as loaded), not from the deck. A study from a
deck has no `data_production`. It takes them from the history events the importer read from the deck's
`WCONHIST` / `WCONINJH` (`well_events`, mode `HISTORY`), which are the same numbers Flow reports as `WOPRH` ...

**Which quantities are matched** depends on how the wells are controlled in history, a choice the well modeler
makes (`control` on the HISTORY events):

| Control | Honoured by Flow | Matched |
| --- | --- | --- |
| `ORAT` (the well modeler's default when a month has oil) | oil rate | water rate, gas rate, BHP (and THP) |
| `LRAT` | liquid rate | water cut, GOR, BHP |
| `RESV` | reservoir voidage | oil, water, gas, BHP |

The matcher reports which control the deck uses, and what it therefore can and can't match.

**Misfit** is a normalised sum of squares per well and quantity:

```
misfit = Σ_wells Σ_quantities w_q · Σ_t ((sim(t) − obs(t)) / σ_q(t))² / N
σ_q(t) = max(rel_q · |obs(t)|, abs_q)    measurement error: relative, with an absolute floor
```

Defaults: rates 10 % with a floor of 1 sm3/d, BHP 5 bar, the same weight for each quantity. They are settable per
study (`hm.json`), as are the period to match and wells to leave out. Alongside the misfit, the report gives
numbers an engineer reads more easily: cumulative error per well and phase, the water breakthrough date, simulated
against observed, and the BHP error at the last measurement.

### 2. Parameters

A match is a search over **parameters**, each one a change a modeler can make to their own model:

| Owner | Parameter | Target | Typical range |
| --- | --- | --- | --- |
| geo | PERMX (and PERMY) multiplier | a zone, a layer range, or a region | 0.1-10, log scale |
| geo | PERMZ (kv/kh) multiplier | a zone | 0.01-1, log scale |
| geo | pore-volume multiplier | a zone | 0.7-1.3 |
| geo | fault transmissibility (MULTFLT) | a fault | 0-1 |
| res | Corey exponents and end points | a SATNUM | nw 1.5-4, now 1.5-4, krw_max 0.1-0.8, Sorw +/- 0.1 |
| res | aquifer size and productivity | an aquifer | volume 0.1-10x, PI 0.1-10x, log scale |
| res | contact depth | an EQLNUM | +/- a few metres |
| well | skin or connection factor | a well | skin -3 to 20 |

Each parameter has a name, owner, target, type (multiplier or value), range, start value and scale (linear or
log). Ranges are the engineer's choices, read back before any search. The matcher never widens a range by itself.

Phases 2-3 cover the geo multipliers, res SCAL and aquifers. Contact depths (they change the initial state, so a
variant would need its own initialisation check) and well skins (local, and quick to over-fit to one well) come
later. Until then, `hm_setup` refuses them.

### 3. Variant decks

A candidate is the approved deck with parameter values applied in memory. The models are read, changed (a
multiplier on PERMX in zone Lower, a Corey exponent, an aquifer PI), and rendered with the deck builder's own
`render()` into `<file_store>/hm/<study>_<n>/case_<k>/`. A variant writes no model version and no `decks` row; its
header says "History-match variant, not for decisions", like the initialisation view. Variants run in Flow, several
at once (`[hm] parallel`, default: cores / 2 with 2 threads each, within `max_parallel`), each with a memory check.

### 4. Search

In three steps, each reported before the next:

1. **Base case.** The approved deck's run: misfit per well and quantity, and plots. Often this alone shows the
   problem (water too early in the south wells, pressure falling too fast).
2. **Sensitivities.** Each parameter at its low and high value, the others at their start values (2N runs, in
   parallel), give a tornado chart of misfit change per parameter. Parameters with no effect are dropped, with your
   agreement.
3. **Optimisation.** Bounded least squares on the weighted residuals (`scipy.optimize.least_squares`, trust region,
   log-scaled where set). The Jacobian comes from finite differences, N + 1 runs per iteration in parallel. It stops
   at a target misfit, a run budget (default 200 runs) or a time budget. For more than about 15 parameters, or to
   get the spread as well as the best case, use an ensemble smoother (ES-MDA, 50-100 members, 4 iterations, all
   members in parallel). That gives a set of matched models instead of one.

The search is restartable: every variant's parameters, misfit and run folder are kept in `hm_runs`, so a stopped
search continues and nothing is run twice.

### 5. The match proposal and review

The matcher's result is a **match proposal**: the parameters and their best values (with flags for any at a bound),
the misfit before and after per well and quantity, and the report. You review it like a model:
`opmagent review hm <proposal>` shows it, and you type `accept`, or reject it with a comment. On acceptance the
matcher raises one study request per owner (`raise_request(name, "geo", "match", "PERMX x 0.62 in zone Lower ...")`).
The modelers build new versions with those values, and say in their assumptions where they came from ("history
match proposal 3"). From there everything is the normal path: review, approval, deck, initialisation gate, Flow run.
The matcher then compares that run with the proposal's best case. They should be equal, since the same values give
the same deck; a difference is a finding.

### 6. Visualisation (the HM report, a QC notebook like the others)

- **Per well:** observed against simulated rates (oil, water, gas, injection) and BHP over time, for the base case
  and the best case, with the measurement band; water breakthrough marked.
- **Misfit map:** misfit per well, base against best, as bars, and on the plan view at the well positions (the
  geo notebook's map style), so regional patterns show.
- **Field:** cumulative oil, water and gas, and field pressure, observed against base and best.
- **Parameters:** a table of start, range, best and the change, with flags at bounds; the tornado chart from the
  sensitivities; parameter maps (for example the PERMX multiplier per zone).
- **Search:** convergence (misfit against run number), and for an ensemble, the spread of each parameter and of
  each well's forecast.
- **In ResInsight:** base and best variants open side by side (`opmagent results open --hm <proposal>`), with the
  difference in pressure and water saturation at the end of history.

### 7. Agent and servers

The `history-matcher` agent uses two servers:

- **The data server, profile `hm`** (read-only on models): reads all four approved models, `data_production`,
  `well_events` and the study, and runs the study task `match` (`start_task`, `complete_task`, `fail_task`,
  `raise_request` for missing data). It can't write any model.
- **A new HM server, `opmagent-hm`**, for everything that runs Flow on variants. Not `opmagent-sim`: that server's
  rule is "only validated decks whose initial state a person accepted", and variants are neither. Keeping them apart
  keeps that rule simple. The HM server reuses `opmagent.sim.runner` to start and watch Flow, and writes only the
  `hm_*` tables and its own folder under `<file_store>/hm/`.

**Capacity.** Ordinary runs and variants share the machine. A variant starts only while
`running runs + running variants ≤ [hm] max_cores / threads per variant`, and only with a memory check (the deck's
estimated memory, from the base run's peak, against free memory). Ordinary runs come first: a study run waiting for a
slot pauses the HM queue until it has started.

### 8. Tables

| Table | One row per | Holds |
| --- | --- | --- |
| `hm_studies` | history match | study, base deck id, base run id, observation source (`data_production` / `deck_history`), period, excluded wells, objective (weights, σ per quantity), status (`setup`, `frozen`, `proposed`, `confirmed`, `abandoned`), revision |
| `hm_parameters` | parameter of an HM study | name, owner (`geo`, `res`, `well`), kind (e.g. `permx_mult`, `corey_nw`, `aquifer_pi`, `multflt`, `skin`), target (JSON: zone / layers / region / SATNUM / aquifer id / fault / well), type (`mult` / `value`), low, high, start, scale (`lin` / `log`), active |
| `hm_jobs` | base, sensitivity or optimisation job | kind, method (`oat`, `lsq`, `esmda`), options JSON (budget runs, budget hours, target misfit), status (`queued`, `running`, `stopped`, `done`, `failed`), pid, progress JSON, started, finished |
| `hm_cases` | evaluated variant | job, case number, parameter values JSON, status, run folder, Flow return code, total misfit, wall time; unique on (HM study, rounded parameter values) so nothing runs twice |
| `hm_misfits` | case × well × quantity | misfit term, N points, cumulative error, breakthrough date (sim / obs), last-BHP error |
| `hm_proposals` | proposal | case, note, at-bound flags, status (`proposed`, `accepted`, `rejected`, `confirmed`, `differs`), reviewer comment, requests raised (ids), confirming run id |

Once a search has run, the setup is `frozen`: parameters, ranges and the objective can't change. Changing them
makes a new revision (`hm_setup` again), which reuses no cases: a misfit is only comparable within one objective.
Summary vectors of variants stay in their run folders (UNSMRY), not in `results`, which belongs to `runs`. The
report and `hm_case` read them from there.

### 9. Tools

All tools return `{ok, ..., spoken_summary}` and fail with `ToolError` like the other servers. New codes:
`NO_HISTORY` (no observations for the study), `SETUP_FROZEN`, `BUSY` (a job is already running for this match).

**Data server, profile `hm`**

| Tool | Does |
| --- | --- |
| `hm_observations(study, wells=None)` | What can be matched, before anything runs: the observation source, the period, per well the quantities with data and how many points, gaps and zero-hour months, the history control (`ORAT`, `LRAT`, `RESV`) and so the quantities that are honoured, not matched. `NO_HISTORY` if there is nothing. |
| `hm_targets(study)` | The valid targets for parameters, from the approved models, with their current values: zones and layer ranges with mean PERMX / PERMZ / PORO, faults with MULTFLT, SATNUMs with their Corey parameters (or "tabulated", which allows only end-point scaling), aquifers with volume and PI, EQLNUMs with contacts, wells with skin. The agent proposes parameters from this; it can't invent a target. |

**HM server**

| Tool | Does |
| --- | --- |
| `hm_resolve(text)` | Find a history match by spoken name ("the Norne match"). |
| `hm_setup(study, parameters, objective=None, period=None, exclude_wells=None)` | Checks and stores the setup: every target exists (`hm_targets`), low < start < high, log scale only for positive ranges, a `value` parameter inside the physical limits the validators use (Corey exponent > 0, Swc + Sorw < 1, contact inside the grid). Returns a `read_back`: "8 parameters: PERMX x 0.1-10 log in zones Upper, Middle, Lower; ... Matching oil, water, gas, BHP for 14 wells, 1997-2006; σ rates 10 % (floor 1 sm3/d), BHP 5 bar." The agent says it and starts nothing until the engineer says yes. On a frozen setup it makes a new revision. |
| `hm_base(hm_id, run_id=None)` | Misfit of the approved deck: the latest stored run of the study's deck, or `run_id`. It runs nothing (the base run goes through the run agent and the initialisation gate as now). Returns the misfit per well and quantity, the five worst terms in words ("water in B-2H arrives 14 months early"), and stores it as case 0. |
| `hm_sensitivity(hm_id, parameters=None)` | Starts a background job: each active parameter at low and high, the rest at start (2N cases). Returns the job id and an estimate (cases × base wall time / parallel slots). |
| `hm_optimise(hm_id, method="lsq", start="best", budget_runs=200, budget_hours=None, target_misfit=None)` | Starts the search. `lsq`: bounded trust-region least squares on the weighted residuals, finite-difference Jacobian (N + 1 cases per iteration, in parallel), log parameters searched in log space. `esmda` (phase 5; refused before then): `members` (default 64) × `iterations` (default 4), all members of an iteration in parallel. `start="best"` begins from the best case so far. `BUSY` if a job is running. |
| `hm_status(job_id)` | Cases done / running / failed / left, best misfit so far and its case, the last iteration's improvement, estimated time left, and for a stall (no case finished for 3 × the base wall time) a warning. |
| `hm_stop(job_id)` | Stops after the running cases finish (or at once with `now=True`, killing them). A stopped job continues with the same `hm_optimise` call: finished cases are reused. |
| `hm_cases(hm_id, order="misfit", limit=10)` | The evaluated cases: number, parameter values, misfit, status. |
| `hm_case(hm_id, case, wells=None, vectors=None)` | One case in detail: misfit terms per well and quantity, and simulated against observed series (downsampled, like `query_results`), read from its run folder. Also failed cases, with the end of the Flow log. |
| `hm_report(hm_id, cases=None)` | Builds the HM report (section 6) for the base, the best and any listed cases, as a QC notebook, and returns its file and the main findings. |
| `hm_propose(hm_id, case, note)` | Makes a match proposal from a case: the parameter changes written as modeler requests ("geo: PERMX x 0.62 in zone Lower (layers 12-18)"), flags for parameters at a bound and for wells whose misfit got worse, and the report. It doesn't raise the requests: that happens when you accept. One open proposal per match: `BUSY` while one is `proposed` or `accepted`; after a rejection the matcher can propose again. The report lists the next best cases (misfit and how their parameters differ) for context only. |
| `hm_confirm(proposal_id, run_id)` | After the modelers' new versions are approved and their deck has run: compares that run with the proposal's case (`compare_runs`-style, `rel_tol` 1e-4, on all matched vectors). Equal: the proposal is `confirmed` and the study's `match` task can complete. Not equal: `differs`, with the vectors and the first time they part; a finding for you and the modelers, not something the matcher fixes. |

**Not tools (yours only):** `opmagent review hm <proposal>` shows the proposal and its report; `accept` raises one
study request per owner (kind `match`, from task `match`) with the exact values, `reject "<comment>"` sends the
comment back to the matcher. The agent can't accept its own proposal, as modelers can't approve their own versions.

### 10. How variants are made

`variant(base_deck, values) -> deck folder`:

1. Load the approved fluid, geo, res and well versions of the base deck as dicts (`models.get_model`).
2. Apply each parameter to its model, the same change a modeler would make: a PERMX multiplier multiplies the PERMX
   array in the target cells; a Corey exponent rebuilds that SATNUM's SWOF from the Corey parameters (the res
   model's own builder); an aquifer PI or a MULTFLT replaces one value; a skin changes the well's connections.
3. Render with the deck builder's `render()` into `case_<k>/`, with the variant header, and the summary keywords
   the matched vectors need.

Because step 2 is the modelers' own change, the rebuilt deck after acceptance is the same deck, which is what
`hm_confirm` checks. A parameter that can only be applied as a deck keyword (for example on passthrough text of an
imported deck) is refused by `hm_setup` until its data has a model home.

### 11. The study task and commands

- **Study chain:** an optional `match` task after `run`: run → match → (requests to geo / res / well) → deck → run
  → match (`hm_confirm`) → view. A study with no history (`hm_observations` says `NO_HISTORY`) skips it. Adding it
  means `'match'` in `study_tasks.task`, `study_requests.kind` gaining `'match'`, and `studies.AGENT['match'] =
  'history-matcher'`. `complete_task` for `match` refuses (`INVALID_INPUT`, "no confirmed match proposal") unless
  the study has a proposal in status `confirmed`. A proposal that `differs` keeps the task open until the
  difference is resolved and a new run confirms it.
- **Commands** (the same functions as the tools, for the engineer at a terminal):
  `opmagent hm observations <study>`, `hm targets <study>`, `hm setup <study> --params hm.json`,
  `hm base <hm>`, `hm sensitivity <hm>`, `hm optimise <hm> [--method esmda] [--budget-runs N]`,
  `hm status <job>`, `hm watch <job>` (live, like `runs watch`), `hm stop <job>`, `hm cases <hm>`,
  `hm report <hm>`, `opmagent results open --hm <proposal>`, and `opmagent review hm <proposal>`.
- **Skill:** `skills/history-matcher/SKILL.md`, in the run agent's style: observations first, propose parameters
  from `hm_targets` with ranges and reasons, read back, base, sensitivity and the tornado, drop dead parameters
  with the engineer's yes, optimise, report, propose. Rules: never widen a range, never propose a case with a
  failed or truncated run, say which quantities the history control makes unmatchable, spoken answers in two or
  three sentences.

### 12. Testing the tools

- **Unit:** misfit on hand-made series (σ floor, zero-hour months skipped, period cut); parameter application per
  kind against the modeler's builder (a variant's PERMX equals the geo modeler's output for the same multiplier);
  `hm_setup` refusals (unknown zone, inverted range, log on a range through 0, frozen setup).
- **Server (no Flow):** a fake runner returning a known analytic response, so `lsq` and `esmda` are tested for
  recovery, budgets, stop and restart (no case run twice) in seconds.
- **Flow:** the synthetic truth of phases 2 and 3 on the hello deck, marked `slow`.

## Plan

| Phase | What | Gate to go on |
| --- | --- | --- |
| 1. Misfit | Observations from `data_production`, misfit per well and quantity on a finished run, the HM report's per-well, field and misfit plots | the hello study with history reports misfit 0 against its own simulated "history" |
| 2. Variants and sensitivities | Parameters, variant decks, parallel Flow runs with memory checks, the tornado chart | a synthetic truth: hello with PERMX x 0.5 in layer 1 and Corey nw 3. With one PERMX multiplier among the parameters, the sensitivity ranks those two first; with one per layer, it reports the three layers as near-equivalent (hello's layers are the same rock, completed in both wells, so the data can't tell them apart) |
| 3. Optimisation and proposal | `least_squares` with finite differences, restartable `hm_runs`, the match proposal and its review, requests to the modelers | the synthetic truth: both parameters recovered within 5 %, the misfit near 0; after the modelers' versions, the Flow run equals the best case |
| 4. Norne | The Norne study from its deck: base misfit against its WCONHIST history, a sensitivity on zone PERMX multipliers, MULTFLT and aquifer support | reported, not a pass mark: the base case is the benchmark's starting model |
| 5. Ensemble (optional) | ES-MDA, the parameter and forecast spread | the synthetic truth inside the ensemble's spread |

**Phase 1 as built** (`opmagent/hm/`, `opmagent/servers/hm_server.py`, `opmagent hm`):

- `opmagent-hm` serves `hm_observations`, `hm_misfit` and `hm_report`, read only. `hm_observations` is on the HM
  server rather than the data server for now: the `deck_history` source reads the run's summary file. The data
  server's `hm` profile and `hm_targets` come with the parameters in phase 2.
- `opmagent setup` doesn't add `opm-hm` to a workspace's `.mcp.json` yet: no agent uses it until the
  `history-matcher` agent and its profile exist (phase 2). Phase 1 is used through `opmagent hm`.
- `hm_misfit(run_id, ...)` is the misfit of any finished run. `hm_base` (case 0 of a setup) wraps it once setups and
  the `hm_*` tables exist (phase 2).
- Observation sources: `data_production` (volumes to monthly rates on producing days, as `monthly_history`),
  `deck_history` (`WOPRH` ... from the run, only for wells with HISTORY events: Flow also writes prediction targets
  into those vectors), and `run` (another run as a synthetic truth).
- Simulated rates are the time-weighted monthly mean of the summary rate over the calendar month.
- The deck builder's default summary gains `WGPR`, `WGIR` and the history vectors `WOPRH`, `WWPRH`, `WGPRH`,
  `WWIRH`, `WGIRH`, `WBHPH`.
**Phase 2 as built** (`opmagent/hm/params.py`, `opmagent/hm/jobs.py`):

- Tables `hm_studies`, `hm_parameters`, `hm_jobs`, `hm_cases`, `hm_misfits` (a migration). Triggers refuse
  changes to a frozen setup's parameters and objective. `hm_proposals` comes with phase 3.
- Parameter kinds: `permx_mult` (with PERMY), `permz_mult`, `poro_mult` by zone, layer range or all cells;
  `multflt`; Corey `corey_nw`, `corey_now`, `corey_ng`, `corey_nog`, `krw_max`, `krg_max`, `sorw`, `swc` per
  SATNUM (rebuilt with the res modeler's `scal_tables`); `aquifer_volume_mult`, `aquifer_pi_mult`. Contacts and
  skins are refused.
- New HM server tools: `hm_targets`, `hm_setup`, `hm_resolve`, `hm_base`, `hm_sensitivity`, `hm_status`, `hm_stop`,
  `hm_cases`, `hm_case` and `hm_match_report`. `hm_targets` is on the HM server, not the data server.
- The `history-matcher` agent uses a new read-only `hm` data-server profile and the `history-matcher` skill.
  `opmagent setup` now adds `opm-hm`.
- `hm_base` uses the deck's latest finished run, never the synthetic truth's. Without one it runs the base deck
  as a variant.
- Capacity is `[hm] parallel` (default: cores / (2 × threads)), less the ordinary runs in progress. A case starts
  only while free memory ≥ `[hm] min_free_mb` plus the running cases' estimate (300 MB + 20 kB per cell, or
  `[hm] case_memory_mb`). A case that runs longer than `[hm] case_timeout_s` is stopped and failed.
- The sensitivity ranks parameters by how much one end lowers the misfit, then by effect. A parameter whose
  effect is under 1 % of the base misfit is listed under `no_effect`.
- Found on the way: the Corey builders produced complex numbers for non-integer exponents (rounding took the
  normalised saturation past 1). They now clamp it to [0, 1].

**Phase 3 as built** (`opmagent/hm/jobs.py` optimiser, `opmagent/hm/proposals.py`):

- `hm_optimise`: bounded least squares (`scipy.optimize.least_squares`, trf). The search space is log10 for log
  parameters. The residuals are fixed for the whole search: sqrt(w / N) · (sim − obs) / σ over the observations the
  objective keeps; a failed case counts as 10 σ everywhere. The Jacobian is forward differences (1 % of each range,
  backward at the upper bound), its N cases one parallel batch. Every evaluation is a case, so a restart reuses
  them. It stops at xtol / ftol, `target_misfit`, `budget_runs` or `budget_hours`, and reports parameters at a
  bound.
- `hm_propose`: the best case's values rounded to 3 significant figures (`digits`) and run as their own case, so
  the proposal is exactly what the modelers will type. The changes are written per owner, with flags at a bound and
  for wells worse than the base. One open proposal per match (a unique index).
- `opmagent review hm show / accept / reject`, the engineer's only. Accept raises one study request per owner (kind
  `match`, from task `match`); without a study it prints the changes. Agents can't raise `match` requests.
- `hm_confirm`: a run of a deck other than the base deck, compared with the proposal's case on every matched
  quantity, monthly (rel_tol 1e-4 of each series' largest value). `confirmed` or `differs`.
- Study task `match` (agent history-matcher) after `run`. It is optional: a study completes without it if it was
  never started. It completes only with a confirmed proposal, or as skipped when the study has no production
  history. Older databases get `study_tasks` and `study_requests` rebuilt with the new CHECK values, and their
  studies (without a match row) keep working.
- Gate: `tests/test_hm_proposals.py`. On hello, from start values PERMX ×1 and nw 2, the search recovers PERMX ×0.5
  and nw 3 (to 1e-5) in 6 iterations, 18 cases. The proposal rounds them to 0.5 and 3. Accepting it sends requests
  to geo and res. New geo, res and well versions with those values are approved, and the deck is rebuilt and run.
  `hm_confirm` finds the run equal to the proposal (largest difference below 1e-6), and the match task completes.
  A rebuild with nw 2.5 is reported as `differs`.

**Phase 4 as built** (Norne: [testing/history-match-norne.md](../testing/history-match-norne.md),
`testing/hm_norne.py`):

- Norne imported, rebuilt and run (292 s). The misfit is against `deck_history` (30 wells, 1997-12 to 2006-11):
  **47.4** over 7,899 monthly values, with σ for a field (rates 10 % with a floor of 20 sm3/d, BHP 10 bar).
  Simulated water arrives early in 16 producers.
- The sensitivity covered PERMX by formation (layer groups: Norne's grid has no geo zones) and MULTFLT on four
  faults: 17 of 17 cases. Most improving: opening fault E_01 (-6.6), lowering Tofte (-6.1), raising Ile (-3.7),
  lowering Tilje (-1.5). No effect: Garn and fault BC.
- Norne has no analytical aquifer and no Corey SCAL (both SATNUMs tabulated), so those parameters don't apply.
- Changes it brought:
  1. Controls are judged month by month (`misfit.honoured_mask`), since Norne's wells switch between ORAT and
     RESV.
  2. A parameter is refused when the deck's passthrough text rewrites its keyword (`params.rewritten`). Norne
     makes PERMZ from PERMX with COPY and MULTIPLY.
  3. Variants keep only their summary (`[hm] keep_grid_output`). A case starts only with `[hm] min_free_disk_mb`
     free disk, and a failed case is tried again by the next job.

- Gate: `tests/test_hm.py` runs hello and gets misfit 0 against itself, both as a `run` truth and through
  `data_production` built from its own monthly volumes. The `deck_history` path has no end-to-end test yet (no small
  deck with WCONHIST); Norne in phase 4 is its test.

## Decisions

1. **Agent:** decided: a new agent, `history-matcher`, with its own profile and skill.
2. **Accepting a match:** decided: one proposal at a time. Other good cases are listed in its report for context.
3. **Parameters first:** decided: geo multipliers, res SCAL and aquifers in phases 2-3. Contact depths and well
   skins come later.
4. **Search:** decided: bounded least squares first (phase 3); the ES-MDA ensemble comes in phase 5.
5. **Server:** decided: a separate `opmagent-hm` server for variant runs, so `opmagent-sim` keeps its "validated
   and accepted decks only" rule.
6. **Confirm step:** decided: `hm_confirm` must pass before the study's `match` task completes.
