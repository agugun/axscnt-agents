# Initialisation view

This scenario checks that the dynamic model (fluid + geo + res + well, put together in a deck) initialises as it
should, and can be seen in ResInsight at time zero, before any simulation:

```
part 1  hello: four demo models ──► HELLO deck ──► initialisation view: fluids in place, contact checks
part 2  SPE9.DATA, Norne ──► study ──► fluid, geo, res, well ──► deck ──► initialisation view
                                                                              │
        the source deck, initialised the same way ──────────────────────────► equal: initial state, fluids in place
part 3  each initialisation view ──► opmagent results open --deck <id> --init ──► ResInsight
```

- **Part 1** builds the hello models and their deck. The deck build makes the initialisation view itself; its
  fluids in place, average pressure and contact checks must be what the hello models say.
- **Part 2** rebuilds SPE9 and Norne through studies, four models and a deck. Each deck's initial state must equal
  the source deck's, initialised the same way: pressure, saturations, Rs (and Rv), fluids in place for the field
  and per FIPNUM region. This proves the four models, put together, are the source's dynamic model at time zero.
- **Part 3** opens each view in ResInsight; you check what you see against the list below.

## What the initialisation view is

The deck is copied with its SUMMARY replaced by the fluids-in-place vectors and its SCHEDULE by one 1e-4 day step
without wells, then run in the release OPM Flow. Report step 0 of the restart file is the equilibrated initial state
(PRESSURE, SWAT, SGAS, RS / RV), and the summary at time 0 holds the fluids in place (FOIP, FGIP, FWIP, FPR, and per
FIPNUM region ROIP, RGIP, RWIP, RPR). Nothing flows. It is not a run: nothing is written to `runs`, and the deck and
its runs are never touched.

- Made after every deck build (it never fails the build), in `<workspace>/data/files/init_views/<deck>_deck<id>/`:
  `INIT_<DECK>.DATA`, `.EGRID`, `.INIT`, `.UNRST`, `.SMSPEC`, `run.log`, `init_view.json`.
- **Checks** against the deck's res model, per EQLNUM: cells clearly below the OWC (by half a cell) are at the
  saturation table's maximum water saturation, and cells clearly below the GOC hold no free gas. The water check is
  "n/a" for tables whose capillary pressure at maximum Sw isn't zero (a transition zone then reaches below the
  contact: SPE9 and Norne). A warning names how far below the contact the deepest such cell is.
- `opmagent deck init <id>` makes it again; `opmagent results open --deck <id> --init` opens it; MCP `init_view` is on
  the deck, run, view and reviewer profiles; `opmagent study view` lists it.

## Success criteria

| # | Check | Pass when |
| --- | --- | --- |
| 1 | The deck build makes the view | the deck result has `init_view`; INIT, UNRST and SMSPEC are written; no run is recorded |
| 2 | The init deck | no wells, says "Not for simulation" |
| 3 | hello (part 1) | FOIP > 0 (about 3.86 million sm3), hydrocarbon pore-volume fraction 0.800 (Swc 0.2, OWC below the grid), average pressure 201 bar, water below the OWC passes |
| 4 | SPE9 (part 2) | 9,000 active cells; initial PRESSURE, SWAT, SGAS, RS, FOIP, FGIP, FWIP, FPR and the region totals equal the source deck's (relative difference ≤ 1e-6) |
| 5 | Norne (part 2) | 44,431 active cells; the same, plus RV, for all 16 FIPNUM regions (≤ 1e-6) |
| 6 | ResInsight (part 3) | each view opens and shows what the list under part 3 describes |

Last run (2026-11-22): all checks pass. SPE9: PRESSURE 1.2e-7, SWAT 1.1e-7, the rest 0; Norne: all 0.

## Before you start

Follow [testing/README.md](README.md#before-any-scenario): opmagent, OPM Flow, the open decks (SPE9, Norne),
ResInsight for part 3.

## A. One script

```bash
testing/init_view.sh                 # parts 1-3 in a new temporary workspace (about 45 s; ResInsight opens 3 times)
NO_VIEWER=1 testing/init_view.sh     # print the ResInsight commands instead of opening them
SKIP_NORNE=1 testing/init_view.sh    # part 2 with SPE9 only
```

Options: `WS=<dir>` (the workspace to create), `OPM_TESTS=<dir>`, `RESINSIGHT=<path>`. It prints PASS or FAIL per
check and ends with `PASS initialisation view`.

## B. Terminal, step by step

Do every step in **one terminal**: `REPO`, `WS`, `OPMAGENT_CONFIG` and `CHECK` exist only in the terminal where you
set them. In a new terminal, set them again (the second block) before going on.

```bash
cd ~/repo/axscnt-agents                 # your clone of this repo
source .venv/bin/activate
REPO=$PWD
WS=~/opmagent-testing/init-view         # a new folder: if it exists from an earlier try, remove it or pick another
opmagent setup $WS
sed -i 's/^auto_approve = false/auto_approve = true/' $WS/opmagent.toml     # or approve each model yourself
sed -i "s|^# resinsight_bin = .*|resinsight_bin = \"$HOME/opm-agents/ResInsight/bin/resinsight\"|" $WS/opmagent.toml
```

```bash
export OPMAGENT_CONFIG=$WS/opmagent.toml
CHECK="python $REPO/testing/init_view_check.py"
opmagent doctor | head -1                     # must show: [ok]   config: database=<WS>/data/opm.db
```

The deck ids below (1, 2, 3) hold for a new workspace; on a rerun use the ids the steps print.

### Part 1: hello

```bash
opmagent hello                                # four models approved (auto-approve), the HELLO deck, its run
$CHECK deck 1 --hc 0.8 --fpr 201 --fpr-tol 2
```
Expect the deck stage to report `Deck HELLO initialises: oil in place 3,855,717 SM3, ... average pressure 201.1
BARSA.`, and only PASS lines from the check, ending `deck 1: opmagent results open --deck 1 --init`.

### Part 2: SPE9 and Norne

**2.1 SPE9** (deck 2):
```bash
D=~/opmagent/open-data/opm-tests/spe9/SPE9.DATA
opmagent study init SPE9_IV --requirement "Initialisation view of SPE9"
opmagent study task SPE9_IV data start
opmagent data from-deck SPE9_IV $D
opmagent study task SPE9_IV data complete
for k in fluid geo res well deck; do opmagent study build $k SPE9_IV --complete; done
$CHECK compare SPE9_IV $D
```
The deck's summary ends `Deck SPE9_IV initialises: oil in place 214,514,736 STB, gas 298,173,568 MSCF, water
214,918,912 STB, average pressure 3,822.1 PSIA.` The compare initialises SPE9.DATA itself the same way (adding INIT,
which SPE9 lacks) and expects PASS for every line.

**2.2 Norne** (deck 3, a few minutes):
```bash
N=~/opmagent/open-data/opm-tests/norne/NORNE_ATW2013.DATA
opmagent study init NORNE_IV --requirement "Initialisation view of Norne"
opmagent study task NORNE_IV data start
opmagent data from-deck NORNE_IV $N
opmagent study task NORNE_IV data complete
for k in fluid geo res well deck; do opmagent study build $k NORNE_IV --complete; done
$CHECK compare NORNE_IV $N
```
Expect `oil in place 160,791,712 SM3 ... average pressure 271.8 BARSA, 16 fluid-in-place regions`, and PASS for
every line, `16 regions, all equal` included.

### Part 3: ResInsight

```bash
opmagent results open --deck 1 --init        # hello
opmagent results open --deck 2 --init        # SPE9
opmagent results open --deck 3 --init        # Norne (after step 2.2)
opmagent deck init 2 --open                  # the same, made again first; prints the checks and region totals
```

What you should see (Cell Result, time step 0):

| Case | In ResInsight |
| --- | --- |
| hello | the 10 x 10 x 3 box; PRESSURE rising with depth from about 200 bar at the top; SWAT 0.2 everywhere (connate water: the OWC at 2100 m is below the grid); SGAS 0 |
| SPE9 | PRESSURE rising with depth around 3,800 psia; SWAT high at the bottom of the reservoir and low above (the OWC at 9,950 ft, with a transition zone); no free gas (the GOC at 8,800 ft is above the reservoir) |
| Norne | the faulted Norne grid at its start: PRESSURE about 272 bar on average, SWAT and SGAS showing oil between each region's contacts and water below; EQLNUM shows the five equilibration regions, FIPNUM the 16 fluid-in-place regions |

Step 1 of the restart (1e-4 day later, no wells) looks the same: nothing flows. The summary shows FOIP, FGIP, FWIP
and FPR as flat lines over that step.

## C. The agents in Claude Code

1. "Build the deck for study SPE9_IV" (the deck builder): its answer includes the fluids in place.
2. "Show the SPE9_IV deck at time zero in ResInsight" (the results viewer): `opmagent results open --deck <id> --init`.
3. "What are the fluids in place for deck 2, per region?" (the run agent or the results viewer): `init_view(2)`.

## If it fails

| What you see | Why | What to do |
| --- | --- | --- |
| `init_view_error: the initialisation view needs the OPM Flow binary` | `flow` not on PATH | install OPM Flow, or set `[paths] flow_bin` |
| `OPM Flow could not initialise the deck: ...` | the deck doesn't initialise (e.g. a pressure outside the PVT tables) | read `init_views/<deck>_deck<id>/run.log`; the same deck would fail its first run |
| a check warns `... the deepest X below the contact` | cells below a contact keep oil or gas | near the contact: a capillary transition or end-point scaling; far below it: look at the contacts and SCAL of the res model |
| `deck <n> not found (database ~/opm-agents/...)` | `OPMAGENT_CONFIG` isn't set in this terminal | `export OPMAGENT_CONFIG=$WS/opmagent.toml`, or `--config $WS/opmagent.toml` |
| `ResInsight not found: set [viewer] resinsight_bin` | the new workspace has no ResInsight path | the `sed` line for `resinsight_bin` in the setup block |

## What the scenario covers

| Step | Code |
| --- | --- |
| deck build makes the view | `opmagent/deck/build.py` (`init_view.after_build`) |
| init deck, Flow run, initial state, checks | `opmagent/deck/init_view.py` (`init_deck_text`, `build_init_view`, `initial_state`, `checks`) |
| opening it | `opmagent/viewer.py` (`find_case(deck=...)`), `opmagent results open --deck --init`, `opmagent deck init` |
| MCP | `init_view` on the deck, run, view and reviewer profiles |
| unit tests | `tests/test_init_view.py` |
