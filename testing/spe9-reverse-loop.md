# SPE9 reverse loop

Two connected scenarios check that the reverse process loses nothing:

```
            scenario 1 (study SPE9_S1)                         scenario 2 (study SPE9_S2)
SPE9.DATA ──► data manager reads the deck ──► input ──────────► data manager takes S1's input
                                              │                     │
                                              ▼                     ▼
                    fluid, geo, res, well modelers     fluid, geo, res, well modelers
                                              │                     │
                                              ▼                     ▼
                         model views (no simulation)        deck builder ──► generated deck
                                                                    │              │
                                                                    ▼              │
                                                            Flow run ──► ResInsight │
                                                                                   │
SPE9.DATA ◄──────────────── success: the same simulation deck, and the same results ┘
```

- **Scenario 1** reads SPE9 into study input. Each modeler builds its model from that input, and you look at each model. Nothing is simulated.
- **Scenario 2** starts a new study from scenario 1's input, not from the deck. The modelers build again, the deck builder renders a deck, Flow runs it, and you open the run in ResInsight.
- **Success** means the deck from scenario 2 is the same simulation deck as `SPE9.DATA`, the input of scenario 1. Its run also gives the same results as SPE9's own run.

## What "the same" means

A rebuilt deck is never the same text: keyword order, number format, include files and comments differ. So the
decks are compared as the simulator reads them. Both are read by the OPM parser (`opmagent deck compare`,
`opmagent study compare`):

| Level | Compared | Pass when |
| --- | --- | --- |
| deck: grid | dimensions, active cells | equal |
| deck: tables | every row of PVTO, PVDG, PVTW, DENSITY, RSVD, ROCK, SWOF, SGOF, EQUIL, WELSPECS, COMPDAT, controls (198 rows in 13 tables for SPE9) | equal, relative tolerance 1e-9 |
| deck: arrays | every cell property as OPM processes it: PORO, PERMX/Y/Z, NTG, multipliers, region numbers and end-point scaling arrays, plus per-cell depth and volume | max relative difference ≤ 1e-6 |
| deck: schedule | start and end date, all 90 report dates, schedule mode, keywords carried verbatim, SUMMARY keywords | equal |
| run: static | INIT arrays (pore volume, permeability, transmissibilities, ...) | ≤ 1e-6 |
| run: init | first restart step (pressure, saturations, Rs) | ≤ 1e-6 |
| run: dynamic | every summary vector at every common time (189 for SPE9) | ≤ 1e-4 |

Keywords that only set cell arrays (`COPY`, `MULTIPLY`, `EQUALS`, a region or end-point array) can be written
differently in the generated deck. They are compared by their final values in the arrays level, not by their text.

Rows of the well tables are compared well by well. The order between wells doesn't change the simulation; the
order within one well (its completions, its controls over time) does, and is kept.

The comparison lists, but doesn't fail on, keywords that only control output. For SPE9 that is the `RPTRST` the
deck builder adds to SOLUTION, so that ResInsight gets restart files.

**What this loop can't show.** A deck holds the modelers' results, not their raw data. There is no way back from
PVT tables to a fluid composition, or from a cell grid to the tops and logs it came from. So the models in this
loop take the deck's tables and arrays as given. The forward methods (EOS fitting, kriging, perforation placement)
are tested elsewhere, in `tests/` and in studies built from raw data.

## Before you start

Follow [testing/README.md](README.md#before-any-scenario): opmagent, Flow, the SPE9 deck, a testing workspace.
The deck is at `~/.local/share/opmagent/open-data/opm-tests/spe9/SPE9.DATA`. The steps below write it as `$DECK`:

```bash
export OPMAGENT_CONFIG=~/opmagent-testing/opmagent.toml
DECK=~/.local/share/opmagent/open-data/opm-tests/spe9/SPE9.DATA
```

If the studies `SPE9_S1` and `SPE9_S2` already exist in that workspace from an earlier test, use new names, e.g.
`SPE9_S1b` and `SPE9_S2b`. Studies are never deleted, so each test starts on clean names.

You can follow the loop in three ways. All of them do the same steps on the same study board:

| Way | Use it when |
| --- | --- |
| **A. One script:** `testing/spe9_loop.sh` | a quick check, e.g. after a change; it makes its own workspace |
| **B. Terminal, step by step** (below) | you want to see each step and its output |
| **C. The agents in Claude Code** (below) | you want to check that the agents follow the same path |

## A. One script

```bash
testing/spe9_loop.sh                       # or: testing/spe9_loop.sh /path/to/SPE9.DATA
NO_VIEWER=1 testing/spe9_loop.sh           # no browser or ResInsight (e.g. over ssh)
RESINSIGHT=/path/to/resinsight testing/spe9_loop.sh
```

The script:
- makes a new temporary workspace, so nothing touches yours
- turns on auto-approval, since there is no reviewer
- runs every step of section B and stops at the first failure
- ends with `PASS SPE9 reverse loop` and prints the workspace path and the two report files

## B. Terminal, step by step

Each step names the agent that does it in a real study. From a terminal you act for that agent. The board
checks each step as it would for an agent: a task completes only with an approved model, a validated deck or a
finished run.

### Scenario 1: SPE9 to input, models and views

**1.1 Start the study** (deck builder).
```bash
opmagent study init SPE9_S1 --requirement "Reverse SPE9: read the deck into study input and build the models"
```

**1.2 Read the deck into the study's input** (data manager).
```bash
opmagent study task SPE9_S1 data start
opmagent data from-deck SPE9_S1 "$DECK" --check     # report only: nothing is written
opmagent data from-deck SPE9_S1 "$DECK"
opmagent data verify SPE9_S1
opmagent study task SPE9_S1 data complete
```
Expect:
- `Read SPE9.DATA: grid 24 by 25 by 15 (9000 active), 26 wells, 198 table rows; schedule verbatim.`
- tables per model:
  - `fluid  pvto 12, pvdg 11, pvtw 1, density 1, rsvd 2`
  - `res  rock 1, swof 30, sgof 10, contacts 1`
  - `well  well_groups 1, wells 26, well_completions 26, well_events 76`
- `All 7 files are as loaded.` These are the source deck and the six cell-array files in `data/deck/load-001/`: PORO, PERMX, PERMY, PERMZ, DZ and TOPS. SPE9's grid is Cartesian, so there is no grid file.

The three warnings are expected:
- 18 keywords are carried verbatim.
- The schedule is verbatim, because SPE9's WELSPECS use items beyond 6.
- The study period comes from the deck: 2015-01-01 to 2017-06-19.

See what is stored: `opmagent data show SPE9_S1 deck`, `opmagent data show SPE9_S1 files`.

**1.3 Build the models from the input** (fluid, geo, res and well modelers; geo before res and well).
```bash
opmagent study build fluid SPE9_S1 --complete
opmagent study build geo   SPE9_S1 --complete
opmagent study build res   SPE9_S1 --complete      # SCAL, rock, contacts: defined on the geo grid, after geo
opmagent study build well  SPE9_S1 --complete      # its completions are on the geo grid: after geo
```
Each build:
- creates a version
- runs the modeler's validator
- writes the model's QC report
- submits it for review

Expect `passed its validator`, with 27, 48 and 129 table rows.
- **With auto-approval:** `approved automatically`, and `--complete` completes the task.
- **Without it:** the build prints the approve command. Approve as the reviewer, then complete the task:
  ```bash
  opmagent review list
  opmagent review approve fluid <version_id>
  opmagent study task SPE9_S1 fluid complete --result '{"version_id": <version_id>}'
  ```

**1.4 Look at each model** (no simulation).
```bash
opmagent study view SPE9_S1 --open      # without --open: just the report paths
```
Each model's QC report opens in the browser. Check:

| Model | Check in its report |
| --- | --- |
| fluid `SPE9_S1_Oil` | Pressure range 14.7 to 5000 psia, max Rs 1.5 Mscf/stb. In the PVTO plots, Bo rises and oil viscosity falls along the saturated curve. PVDG: Bg falls steeply at low pressure. PVTW Bw 1.0034, densities 44.99 / 63.02 / 0.0704 lb/ft3. |
| geo `SPE9_S1_Geo` | Grid 24 x 25 x 15, 9,000 active cells. PORO from 0.08 to 0.17, constant within each layer. PERMX log-normal in its histogram, from 0.003 to about 10,000 mD (mean 108 mD). TOPS map of the top layer. |
| res `SPE9_S1_Res` | SWOF and SGOF curves (one SATNUM). EQUIL: datum 9035 ft at 3600 psia, OWC 9950 ft, GOC 8800 ft. ROCK at 3600 psia. |
| well `SPE9_S1_Wells` | 26 wells: 25 producers and 1 injector (INJE1 at 24,25). In the well map, the producers are spread over the grid and the injector is in the corner. Completions: producers in K 2-4, the injector in K 11-15. Schedule plot: oil targets of 1500 stb/d, with the cut to 100 and back. |

Scenario 1 passes when:
- the data task and all four model tasks are done (`opmagent study status SPE9_S1`)
- each model has a QC report you could check against the table above

### Scenario 2: the input to a deck, a run and ResInsight

**2.1 Start the second study and give it scenario 1's input** (deck builder, then data manager).
```bash
opmagent study init SPE9_S2 --requirement "Rebuild SPE9 from scenario 1's input and run it"
opmagent study task SPE9_S2 data start
opmagent data from-study SPE9_S2 SPE9_S1 --check
opmagent data from-study SPE9_S2 SPE9_S1
opmagent study task SPE9_S2 data complete
```
Expect: `Took study SPE9_S1's input (SPE9.DATA): 198 table rows and 6 grid and array files, all checked`.

The deck is not read again:
- **Tables:** copied row for row from SPE9_S1.
- **Array files:** copied into `SPE9_S2/data/deck/load-001/`, after checking each one is still the file SPE9_S1 loaded. If one changed, the copy stops and names the file.

**2.2 Build the models again, from that input** (modelers).
```bash
opmagent study build fluid SPE9_S2 --complete
opmagent study build geo   SPE9_S2 --complete
opmagent study build res   SPE9_S2 --complete
opmagent study build well  SPE9_S2 --complete
opmagent study view SPE9_S2             # optional: the same views as scenario 1
```
Expect the same row counts as in 1.3: 27, 48 and 129.

**2.3 Build the deck** (deck builder).
```bash
opmagent study build deck SPE9_S2 --complete
```
Expect `Deck SPE9_S2 is built and passed the OPM parser.` The deck is in
`<workspace>/data/files/decks/SPE9_S2/SPE9_S2.DATA`.

**2.4 Success criterion 1: the same simulation deck** (run agent; no simulation).
```bash
opmagent study compare SPE9_S2 --deck-only
```
Expect:
```
The decks are the same simulation deck: grid, 198 table rows in 13 tables, 6 cell arrays and the schedule (90 report dates) match.
  deck grid      same
  deck tables    same
  deck arrays    same
  deck schedule  same
```
The report is in `<study>/deck/compare_with_source_deck.json`. To compare any two decks yourself:
`opmagent deck compare "$DECK" <workspace>/data/files/decks/SPE9_S2/SPE9_S2.DATA`.

**2.5 Run the deck** (run agent).
```bash
opmagent study run SPE9_S2              # in another terminal: opmagent runs watch <run id>
```
Expect `Run <id> finished and stored; the run task is done. Stored 189 vectors ... FOPR 1.21e+04.`

**2.6 Success criterion 2: the same results** (run agent).
```bash
opmagent study compare SPE9_S2
```
This runs `SPE9.DATA` itself once, into `SPE9_S2/data/deck/load-001/run_source/`. It then compares the decks
again, and the runs. Expect the four `deck ... same` lines, then:
```
  run  static    same
  run  init      same
  run  dynamic   same
```
The report is in `<study>/runs/compare_with_source.json`.

**2.7 Look at the run in ResInsight** (results viewer).
```bash
opmagent results open --study SPE9_S2   # --dry-run: print the command instead
```
In ResInsight, check:
- the grid (24 x 25 x 15)
- PORO and PERMX in the static properties
- PRESSURE and SWAT over time, around INJE1 in the corner
- the summary plot of FOPR and FWCT, and the well vectors of the PRODU wells and INJE1

To see that the original behaves the same, open its run too:
`opmagent results open --case <workspace>/data/files/studies/SPE9_S2/data/deck/load-001/run_source`.

Scenario 2 passes when 2.4 and 2.6 both print `same` for every level.

## C. The agents in Claude Code

Same loop, with the agents doing each step. Start `claude` in your testing workspace and talk to the coordinator
(the `study` skill):

1. *"Make a study SPE9_S1 from `~/.local/share/opmagent/open-data/opm-tests/spe9/SPE9.DATA`. Build the models but don't build a deck or run anything."*
   The coordinator reads back the name and the deck. After your "yes":
   - the data manager runs `load_deck_data`, a check first
   - the modelers build: fluid and geo, then well
2. If approval is on, approve the four models when asked (`opmagent review approve ...` in a terminal).
3. *"Show me the SPE9_S1 models."* The coordinator gives you `opmagent study view SPE9_S1 --open`. Check the reports as in 1.4.
4. *"Make a study SPE9_S2 from SPE9_S1's input and run it through to ResInsight."*
   - The data manager uses `load_study_input`.
   - The modelers build, and the deck builder renders the deck.
   - The run agent compares the decks (`--deck-only`).
   - After your "yes", the run agent runs the deck and compares the runs.
   - The results viewer opens ResInsight.
5. Check the board, then check the same success criteria as in section B:
   - `opmagent study status SPE9_S2`
   - `opmagent study events SPE9_S2`: look for the two `compared` events, both with `"passed": true`

An agent may take a different route to the same place, e.g. calling an MCP tool where section B uses the CLI.
The board, the versions and the two compare reports are what count.

## If it fails

| What you see | Likely cause | What to do |
| --- | --- | --- |
| `deck not found` | open data not fetched | `tools/fetch_open_data.sh` |
| `the fluid data isn't ready` on a build | the data task didn't load, or loaded raw data instead | rerun 1.2; `opmagent data show <study>` must list a `deck` dataset |
| `... is qc_ready: approve it first` | approval is on | approve (1.3), then `study task ... complete` |
| `build the geo model first` | well built before geo | complete the geo task, then build well |
| `... changed since it was loaded` | a file in `data/deck/load-NNN/` was edited | reload: `opmagent data from-deck <study> "$DECK"` (it makes load-002) and rebuild the models |
| `has no input read from a deck` on `from-study` | the source study loaded raw data | `from-study` copies deck input only |
| `deck tables DIFFERENT` | a table didn't survive (importer, model or renderer) | the difference names the table, row and columns; check the deck-sourced build and `opmagent/deck/render.py` |
| `deck arrays DIFFERENT` | a cell array changed (e.g. number formatting) | check `coverage.inexact_cells` in `opmagent data show <study> deck` |
| `deck schedule DIFFERENT` | dates or verbatim keywords differ | the difference lists the keywords; compare the SCHEDULE sections of both decks |
| decks `same` but `run ... DIFFERENT` | Flow treats something differently that the parser doesn't show (e.g. output or tuning) | report it, with both JSON reports: it is a gap in the deck comparison |
| `ResInsight not found` | path not set | `[viewer] resinsight_bin` in `opmagent.toml`, or `RESINSIGHT=` for the script |
| `No display` | no desktop session | `--dry-run`, and open the printed EGRID on a desktop |

## What the loop covers

| Step | Code |
| --- | --- |
| deck → input | `opmagent/deck/importer.py`, `opmagent/data/deck_source.py:load_deck` |
| input → input | `deck_source.copy_input` |
| input → models | `deck_source.build_model`, validators, `opmagent/reports/deck_model_report.py` |
| models → deck | `deck_source.deck_spec`, `opmagent/deck/build.py`, `opmagent/deck/render.py` |
| deck = deck | `opmagent/deck/deck_compare.py` |
| run = run | `opmagent/deck/roundtrip.py`, `deck_source.compare_with_source` |

The same chain runs in `pytest` on the hello deck (`tests/test_deck_reverse.py::test_two_scenarios_input_reused_and_deck_the_same`).
With `OPM_TESTS_DIR` set, it also runs on SPE1, SPE9 and Norne.
