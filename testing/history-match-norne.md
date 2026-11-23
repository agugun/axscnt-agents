# History matching on Norne

Norne (opm-tests `norne/NORNE_ATW2013.DATA`) is a public history-matching benchmark: a real North Sea field,
46 x 112 x 22 cells, 30 wells and production history from 1997 to 2006 in its own `WCONHIST` / `WCONINJH`.
This scenario runs the history matcher on it, through the same steps an engineer takes:
1. Import the deck into four models, approve them, rebuild the deck and run it.
2. Measure its misfit against that history.
3. Run a sensitivity on PERMX per formation and the transmissibility of four faults.

It is **reported, not a pass mark** (phase 4 of [docs/design/history-match.md](../docs/design/history-match.md)):
the deck is the benchmark's starting model, so there is no right answer to recover. It passes if every step runs,
every case finishes and the numbers are explained.

## Run it

```bash
python testing/hm_norne.py --out ~/opmagent-testing/hm_norne --parallel 3
```

It takes about 40-45 min on 16 cores: the base run (about 5 min), then 17 cases, 3 at a time, about 5 min each.
`--skip-sensitivity` stops after the base misfit and report. A rerun with the same `--out` reuses everything
finished: the import, the base run and every case. A case that failed (a full disk, a stopped machine) is tried
again.

What you get:
- `hm_norne.json`: the numbers below.
- The base HM report: observed against simulated per well, misfit per well, field cumulatives.
- The match report: the sensitivity tornado, the parameter table and the best case against the base case.

## What it does

| Step | With | Result (2026-11-23, 16 cores, OPM Flow 2025.10) |
| --- | --- | --- |
| Import, approve, rebuild, accept the initial state | `import_to_drafts`, `build_deck` | 3 s, deck `NORNE_HM`; schedule carried verbatim |
| Base run | `run_simulation` | 292 s |
| Observations | `hm_observations` (source `deck_history`) | 30 wells, 1997-12 to 2006-11; controls ORAT and RESV, judged month by month |
| Targets | `hm_targets` | 22 layers (no geo zones: an imported grid), 61 faults, 2 SATNUMs, both tabulated (no Corey parameters), no analytical aquifer |
| Base misfit | `hm_misfit` with σ: rates 10 % (floor 20 sm3/d, gas 5000), BHP 10 bar | **47.4** over 7,899 monthly values |
| Sensitivity | 4 PERMX multipliers (x 0.3-3, log) by formation, 4 MULTFLT (0.001-1, log) | 17 of 17 cases, 3 at a time, about 5 min each |

The formations are layer groups: Garn 1-3, Ile 5-11, Tofte 12-14, Tilje 15-22. Not (layer 4) is a shale, so it
has no multiplier. The faults are BC, DI, C_01 and E_01: large faults with a multiplier below 1.

### Base case

Water is the problem. In 16 producers the simulated water arrives before the observed water, by 2 to 41 months.
In D-4AH it never arrives in the simulation. Cumulative water is too high in D-1CH (+545 %), E-3AH (+507 %), E-2H (+80 %) and B-3H (+57 %). Oil is
matched wherever the well is under RESV control. In its ORAT months it is the control, so it isn't counted.

### Sensitivity (misfit change from 47.4)

| Rank | Parameter | Low end | High end | Most improving |
| --- | --- | --- | --- | --- |
| 1 | MULTFLT E_01 | 65.4 | 40.8 | open the fault: -6.6 |
| 2 | PERMX Tofte | 41.3 | 67.5 | lower: -6.1 |
| 3 | PERMX Ile | 72.7 | 43.7 | raise: -3.7 |
| 4 | PERMX Tilje | 45.9 | 73.4 | lower: -1.5 |
| 5 | MULTFLT DI | 47.6 | 46.7 | -0.7 |
| 6 | PERMX Garn | 47.5 | 47.3 | no effect |
| 7 | MULTFLT C_01 | 50.3 | 51.0 | either way worse (its 0.01 is already best) |
| 8 | MULTFLT BC | 47.4 | 47.4 | no effect |

Three things to read from it:
- **Fault E_01 matters most.** Opening it (0.01 to 1) lowers the misfit by 6.6, and closing it raises it by 18.
- **Ile and Tofte pull in opposite directions.** Ile wants more permeability, Tofte less. Next comes a search on
  these four (E_01, Tofte, Ile, Tilje), not on the eight.
- **Garn and BC can be dropped,** with the engineer's yes.

## Found on the way

Norne showed four things the small decks couldn't. Each is now in the code:

1. **Controls change over time.** Norne's producers switch between ORAT and RESV. A quantity counts as honoured
   (given to Flow, so not matched) only in the months its control gives it. Before this, oil was left out for the
   whole history of any well that was ever under ORAT. B-1H, for one, has 28 ORAT months and 80 RESV months.
2. **The deck can rewrite a keyword itself.** Norne makes PERMZ inside its GRID section: `COPY 'PERMX' 'PERMZ'`,
   then a `MULTIPLY` per layer, carried as passthrough text. A PERMZ multiplier in the model would never reach
   Flow, so `hm_setup` refuses it and says why. A PERMX multiplier does reach PERMZ, through the same `COPY`, as a
   modeler's PERMX change would. The same check covers MULTFLT, PORO and any other keyword the passthrough sets.
3. **Restart files fill the disk.** A Norne variant writes about 300 MB, nearly all of it restart output. The
   first run filled a 7 GB disk and 8 cases failed. Variants now keep only their summary
   (`[hm] keep_grid_output = true` keeps everything). A case starts only with `[hm] min_free_disk_mb` free, and
   the next job tries a failed case again.
4. **σ must suit the field.** The default rate floor of 1 sm3/d is right for the hello box but makes Norne's water
   terms reach 10⁵. Set σ in the objective for a real field.

## Recording a result

Note the date, the commit, the base misfit, the sensitivity ranking, and how many cases finished.
A case that failed is a FAIL: give its `hm_case` error.
