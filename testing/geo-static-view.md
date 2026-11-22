# Geo static view

This scenario checks that the geo modeler's static model can be seen in ResInsight before any simulation:

```
part 1  tops + LAS + geo.json ──► geo modeler ──► geo model ──► static deck ──► OPM Flow dry run ──► EGRID + INIT
part 2  SPE9.DATA, Norne ──► study input ──► geo model, res model ──► static view ──► compared with the deck's own INIT
part 3  each static view ──► opmagent results open --geo <version> ──► ResInsight
```

- **Part 1** builds a geo model from raw data: four wells, three surfaces, LAS logs and a grid spec. The build
  makes the static view itself, and its INIT must hold the model's own arrays, cell by cell.
- **Part 2** reads SPE9 and Norne into studies and builds their geo and res models from the decks. Their static
  views must give the same INIT as each deck's own dry run: pore volumes, permeabilities, transmissibilities and
  regions. This proves the static view is the model as OPM sees it, not an approximation.
- **Part 3** opens each static view in ResInsight; you check what you see against the list below.

## What the static view is

A geo model holds the grid and cell properties, not a simulation. To show it in ResInsight it is written as a deck:
the same GRID section the deck builder writes for real decks, plus small placeholder fluid, SCAL and EQUIL tables
only because OPM Flow won't load a deck without them. They change no static array. OPM Flow's dry run
(`--enable-dry-run=true`) loads the deck, writes EGRID and INIT, and stops without simulating, in a few seconds.

- With a res model on the same geo version, its SATNUM, EQLNUM, FIPNUM and SWATINIT are included. Building the
  res model remakes the geo's static view; `results open --geo` also remakes it when the res model changed.
- A geo model rebuilt from a deck also carries that deck's GRID passthrough (for Norne: MULTREGT, NEWTRAN,
  GRIDOPTS), so transmissibilities are the deck's.
- The static deck says "Not a simulation deck" and is never registered as a deck (decks need four approved models).

Files: `<workspace>/data/files/qc/geo_<model>_v<N>/static/`: `STATIC_<MODEL>_V<N>.DATA`, `.EGRID`, `.INIT`,
`run.log`, `view.json` (which res version it includes).

## Success criteria

| # | Check | Pass when |
| --- | --- | --- |
| 1 | The geo build makes the static view | the build result has `static_view` (an EGRID path); no `static_view_error` |
| 2 | The view holds the model (part 1) | INIT has PORV, DEPTH, DX/DY/DZ, PORO, PERMX/Y/Z, NTG, TRANX/Y/Z; grid 10 x 10 x 5, 500 active cells; INIT PORO, PERMX and NTG equal the model's arrays (relative difference ≤ 1e-5) |
| 3 | It is not a deck | the static deck says "Not a simulation deck"; the `decks` table is empty |
| 4 | SPE9 (part 2) | 9,000 active cells; PORV, DEPTH, PORO, PERMX/Y/Z, NTG, TRANX/Y/Z, SATNUM, EQLNUM, FIPNUM, PVTNUM equal SPE9's own INIT (relative difference ≤ 1e-6) |
| 5 | Norne (part 2) | 44,431 active cells; the same arrays equal Norne's own INIT (≤ 1e-6) |
| 6 | ResInsight (part 3) | each view opens and shows what the list under part 3 describes |

Last run (2026-11-22): all checks pass; the largest difference is TRANX in SPE9, 2.4e-7 (floating-point order).

## Before you start

Follow [testing/README.md](README.md#before-any-scenario): opmagent, OPM Flow (the static view needs the `flow`
binary), the open decks (SPE9, Norne), ResInsight for part 3. `testing/geo_static_check.py` does the checks; it
reads the workspace from `OPMAGENT_CONFIG`.

## A. One script

```bash
testing/geo_static_view.sh                 # parts 1-3 in a new temporary workspace (about 25 s, ResInsight opens 3 times)
NO_VIEWER=1 testing/geo_static_view.sh     # print the ResInsight commands instead of opening them
SKIP_NORNE=1 testing/geo_static_view.sh    # part 2 with SPE9 only
```

Options: `WS=<dir>` (the workspace to create), `OPM_TESTS=<dir>` (the opm-tests checkout),
`RESINSIGHT=<path>` (the launcher). It prints PASS or FAIL per check and ends with `PASS geo static view`.

## B. Terminal, step by step

Do every step in **one terminal**: the variables set here (`REPO`, `WS`, `OPMAGENT_CONFIG`, `CHECK`) exist only in
the terminal where you set them. In a new terminal, set them again (the second block) before going on.

```bash
cd ~/repo/axscnt-agents                 # your clone of this repo
source .venv/bin/activate               # opmagent and the checks run in its environment
REPO=$PWD
WS=~/opmagent-testing/geo-view          # a new folder: if it exists from an earlier try, remove it or pick another
opmagent setup $WS
sed -i 's/^auto_approve = false/auto_approve = true/' $WS/opmagent.toml     # or approve each model yourself
# a new workspace doesn't know where ResInsight is: point it at your launcher (part 3 needs it)
sed -i "s|^# resinsight_bin = .*|resinsight_bin = \"$HOME/opm-agents/ResInsight/bin/resinsight\"|" $WS/opmagent.toml
grep resinsight_bin $WS/opmagent.toml        # must show the path, without a leading #
```

```bash
export OPMAGENT_CONFIG=$WS/opmagent.toml      # every opmagent command below uses this workspace
CHECK="python $REPO/testing/geo_static_check.py"
opmagent doctor | head -1                     # must show: [ok]   config: database=<WS>/data/opm.db
```

If `opmagent doctor` shows `~/opm-agents/data/opm.db` instead, `OPMAGENT_CONFIG` isn't set in this terminal: the
builds would go to your main workspace and `results open --geo <n>` would say `geo version <n> not found`.
The version ids below (1, 2, 3) hold for a new workspace; on a rerun use the ids the steps print.

### Part 1: from raw data

**1.1 The dataset.**
```bash
$CHECK inputs $WS/inputs
```
Expect: `wrote .../tops.csv, .../A.las B.las C.las D.las, .../geo.json (grid 10 x 10 x 5)`.

**1.2 Build the geo model.**
```bash
opmagent geo build --tops $WS/inputs/tops.csv --logs $WS/inputs/*.las --spec $WS/inputs/geo.json --name GEO_VIEW_TEST
```
Expect `"status": "approved"` (or `qc_ready` with approval on), `"static_view": ".../static/STATIC_GEO_VIEW_TEST_V1.EGRID"`,
and a spoken summary ending `Static view for ResInsight: opmagent results open --geo 1.`

**1.3 Check it.**
```bash
$CHECK model GEO_VIEW_TEST
```
Expect only PASS lines: static view made, INIT written, marked not for simulation, not a deck, INIT holds the static
arrays, grid 10 x 10 x 5, 500 of 500 cells active, INIT PORO / PERMX / NTG equal the model's (difference about 1e-7).

### Part 2: from decks

**2.1 SPE9 into a study, geo and res built.**
```bash
D=~/opmagent/open-data/opm-tests/spe9/SPE9.DATA
opmagent study init SPE9_GV --requirement "Static view of SPE9"
opmagent study task SPE9_GV data start
opmagent data from-deck SPE9_GV $D
opmagent study task SPE9_GV data complete
opmagent study build geo SPE9_GV --complete       # makes the static view
opmagent study build res SPE9_GV --complete       # remakes it with the res model's regions
```
Expect the geo summary to end `Static view for ResInsight: opmagent results open --geo <n>.` and the res summary
`The geo static view now shows its regions: ...`.

**2.2 Compare with SPE9's own INIT.**
```bash
$CHECK compare SPE9_GV $D
```
It dry-runs a copy of SPE9's folder (adding `INIT` after `GRID`, which SPE9 lacks) and compares. Expect
`active cells: 9000 (deck: 9000)` and PASS for every array; TRANX shows about 2e-7, the rest 0.

**2.3 Norne.** It becomes geo version 3 (part 3 opens it as `--geo 3`).
```bash
N=~/opmagent/open-data/opm-tests/norne/NORNE_ATW2013.DATA
opmagent study init NORNE_GV --requirement "Static view of Norne"
opmagent study task NORNE_GV data start
opmagent data from-deck NORNE_GV $N
opmagent study task NORNE_GV data complete
opmagent study build geo NORNE_GV --complete      # summary ends: opmagent results open --geo 3.
opmagent study build res NORNE_GV --complete
$CHECK compare NORNE_GV $N
```
Expect `active cells: 44431 (deck: 44431)` and PASS for every array, EQLNUM and FIPNUM included (Norne has several
equilibration and fluid-in-place regions; they come with the res model). Norne takes about a minute.

### Part 3: ResInsight

```bash
opmagent results open --geo 1        # part 1's model (the version id printed by the checks)
opmagent results open --geo 2        # SPE9
opmagent results open --geo 3        # Norne (after step 2.3)
opmagent geo view 2 --open           # the same, remade first
```

What you should see:

| Case | In ResInsight |
| --- | --- |
| part 1 | a 10 x 10 x 5 box dipping east (cell depths 2009 to 2061 m); 5 layers in two zones; PORO 0.178 to 0.220, lower in the west (wells A, C) and higher in the east (wells B, D); PERMX 267 to 574 mD, following PORO |
| SPE9 | 24 x 25 x 15 cells, all 9,000 active; depths 9010 to 10526 ft (FIELD units); PORO constant in each layer (0.08 to 0.17); PERMX log-normal within each layer |
| Norne | the Norne field's faulted corner-point grid, 44,431 active cells, depths 2467 to 3062 m; PORO 0.13 to 0.35, PERMX up to about 4000 mD; FIPNUM and EQLNUM showing several regions |

For each: in the Project Tree, open the case's **Cell Result** and pick PORO, PERMX, NTG, PORV, DEPTH, TRANX or a
region array. There is no time slider and no summary data: the static view has one state and no simulation.

## C. The agents in Claude Code

In your own session in the workspace:

1. "Build the geo model GEO_VIEW_TEST from the files in `inputs/`" (the geo modeler). It reports the static view.
2. "Show it in ResInsight" (the results viewer). It runs `opmagent results open --geo <n>`.
3. For a study: "start a study SPE9_GV from SPE9.DATA", "build geo and res for SPE9_GV", then "show the SPE9_GV geo
   model in ResInsight".

The checks of part 2 are best run with `testing/geo_static_check.py compare`; ask the agent to run it, or run it
yourself.

## If it fails

| What you see | Why | What to do |
| --- | --- | --- |
| `static_view_error: the static view needs the OPM Flow binary` | `flow` not on PATH | install OPM Flow, or set `[paths] flow_bin` |
| `OPM Flow could not load the static deck: ...` | Flow refused a keyword | read `static/run.log`; a passthrough keyword that needs more than RUNSPEC sizing is the usual cause |
| EQLNUM or FIPNUM differ, everything else passes | the view was made before the res model | build the res model, or `opmagent results open --geo <n>` (remakes it) |
| TRANX/Y/Z differ, PORV passes | a deck's GRID passthrough is missing from the view | the geo model must come from the study (`data_source.study`); check `static/*.DATA` for MULTREGT and the like |
| `geo version 3 not found (database <WS>/...)` | step 2.3 (Norne) wasn't run in this workspace | run step 2.3; it makes geo version 3 |
| `geo version <n> not found (database ~/opm-agents/...)` | `OPMAGENT_CONFIG` isn't set in this terminal | `export OPMAGENT_CONFIG=$WS/opmagent.toml`, or add `--config $WS/opmagent.toml` |
| `can't open file .../testing/geo_static_check.py` | the check was run outside the repo folder | use `$CHECK` as set above, or the full path to the file |
| `a study named SPE9_GV already exists` | a rerun in the same workspace | a new `WS`, or a new study name |
| `ResInsight not found: set [viewer] resinsight_bin` | the new workspace has no ResInsight path | the `sed` line for `resinsight_bin` in the setup block, with your launcher's path |
| `Nothing to open` / ResInsight doesn't start | `[viewer] resinsight_bin` not set, or no desktop session | set it in `opmagent.toml`; ResInsight needs a display |

## What the scenario covers

| Step | Code |
| --- | --- |
| geo build makes the view | `opmagent/geo/pipeline.py`, `opmagent/data/deck_source.py` (`after_build`) |
| static deck, dry run | `opmagent/geo/static_view.py` (`render_static`, `build_static_view`) |
| GRID section shared with real decks | `opmagent/deck/render.py` (`grid_section`) |
| res build remakes it | `opmagent/res/pipeline.py`, `deck_source.py` (`after_res_build`) |
| opening it | `opmagent/viewer.py` (`find_case(geo=...)`, `current_case`), `opmagent results open --geo`, `opmagent geo view` |
| MCP | `static_view` on the geo, view and reviewer profiles |
| unit tests | `tests/test_static_view.py` |
