# axscnt-agents

Agent system for OPM Flow reservoir simulation (black oil). For each **study**, a data manager prepares the raw data; fluid, geo, res (reservoir engineering) and well modeler agents build versioned models from it; you approve each one (or turn on auto-approve); a deck builder turns approved models into validated OPM decks; a run agent runs them and stores results; a results viewer opens them in ResInsight. A deck importer reverse-engineers existing decks with a round-trip check, and a study can be started from an existing deck: its data becomes the study input, the agents rebuild it, and the deck comparison checks that the generated deck is the same simulation deck.

Version 0.3.0: see [CHANGELOG.md](CHANGELOG.md). 0.3 needs a fresh database (see the CHANGELOG).

Design: see the architecture and user-guide docs (links in `docs/README.md`).

## Status

| Agent / part | How it runs | State |
| --- | --- | --- |
| Database + human approval gate (optional auto-approve) | SQLite, triggers | done, tested |
| Data server (profiles: fluid, geo, res, well, import, deck, run, data, view, reviewer) | MCP `opmagent-data` | done, tested |
| Studies: task board, requests between agents, coordinator skill | `opmagent study`, `study` skill | done, tested |
| Data manager (study inbox -> modeler input formats) | `opmagent data check / load / show / verify` | done, tested |
| Reverse process: a study from an existing deck, or from another study's input | `opmagent data from-deck / from-study` | done, tested |
| Model views before simulation (fluid, geo, res, well QC reports from deck data) | `opmagent study view` | done, tested |
| Deck comparison (same simulation deck, without a run) | `opmagent deck compare`, `study compare --deck-only` | done, tested |
| Results viewer (ResInsight) | `opmagent results open` | done, tested |
| Fluid modeler (NeqSim, Excel lab report) | MCP `opmagent-pvt` + `opmagent fluid build` | done, tested |
| Geo modeler (tops + LAS -> corner-point grid, kriging, QC notebook) | `opmagent geo build / qc / investigate` | done, tested |
| Res modeler (SCAL, rock, contacts and initial pressure, region arrays on a geo grid) | `opmagent res build / qc / investigate` | done, tested (SCAL workbook, analytical aquifers, QC notebook) |
| Well modeler (surveys, perforations, history incl. Volve, controls, QC notebook) | `opmagent well build / qc / investigate` | done, tested |
| Deck builder | `opmagent deck build` | done, tested |
| Run agent (runs, results, compare, VS Code debug) | MCP `opmagent-sim` | done, tested |
| Deck importer + round trip (SPE1, SPE9, Norne) | `opmagent import deck / verify` | done, tested |
| `hello` end-to-end demo | `opmagent hello` | done, tested |
| Local setup for Claude Code | `opmagent setup` | done |
| Testing scenarios (SPE9 reverse loop) | `testing/spe9_loop.sh`, [testing/](testing/README.md) | done, run on SPE9 |

## Laptop setup (Ubuntu)

Two scripts do it, from a clone of this repo:

```bash
git clone https://github.com/agugun/axscnt-agents.git && cd axscnt-agents
tools/install.sh                     # system packages and OPM Flow (sudo), .venv with opmagent, the open test decks
tools/create_workspace.sh ~/opm-agents   # the agents' workspace, checked with opmagent doctor, then hello
```

- `tools/install.sh` (safe to run again): `sudo apt` installs Python venv, git, Java 17 (NeqSim) and gdb, and OPM
  Flow from the OPM packages (skipped if `flow` is already there); creates `.venv` and runs
  `pip install -e ".[dev,geo,opm]"`; fetches SPE1, SPE9 and Norne into `~/.local/share/opmagent/open-data` (`tools/fetch_open_data.sh`).
  The venv is made from the system `/usr/bin/python3` (a conda or pyenv Python breaks the venv when it's updated);
  `opmagent` is linked into `~/.local/bin`, so it works without activating the venv.
  If ResInsight isn't found, it installs the latest release into `~/.local/opt/ResInsight`
  (`tools/install_resinsight.sh`, below).
  `SKIP_APT=1` (no sudo), `SKIP_DATA=1`, `RUN_TESTS=1` (the test suite at the end), `PYTHON=<python 3.11+>`,
  `NO_LINK=1`, `SKIP_RESINSIGHT=1`.
- `tools/create_workspace.sh [folder]` (default `~/opm-agents`; the folder must not exist yet): `opmagent setup`
  (config, database, file store, `.mcp.json`, `.claude/agents` and skills, `CLAUDE.md`, `reviewer-mcp.json`), then
  `opmagent doctor` and `opmagent hello`. `AUTO_APPROVE=1` (models that pass their validator are approved; hello then
  runs to the end), `APPROVE_SERVERS=1` (pre-approve the MCP servers for Claude Code), `RESINSIGHT=<launcher>` (if it
  isn't on PATH or in `/opt/ResInsight`), `SKIP_HELLO=1`. Without auto-approve, hello stops for your review:
  `opmagent review approve-hello` (you type `approve`), then `opmagent hello` again. On a new workspace the doctor warns
  "server not approved" for every agent until Claude Code approves the servers at its first start (it asks once per
  server), or `APPROVE_SERVERS=1` approves them up front. Answered "don't use", the servers are recorded as
  disabled in `.claude/settings.local.json`, the doctor fails every agent, and `/mcp` no longer lists them: close
  Claude Code, rename `disabledMcpjsonServers` to `enabledMcpjsonServers` in that file, and start it again.
- The first workspace becomes the **default workspace**: `~/.config/opmagent/opmagent.toml` links to its config, so
  `opmagent` works on it from any folder (the config is found from `--config`, then `OPMAGENT_CONFIG`, then
  `opmagent.toml` in the current folder, then the default). A later workspace leaves the link as it is
  (`NO_DEFAULT=1` skips it); `opmagent doctor` says which config it uses and why.
- `opmagent setup` writes `.claude/settings.json` allowing `opmagent` commands and the workspace's MCP tools, so the
  agents don't ask for each call. The reviewer is never allowed: approvals stay with you.

### Upgrade

```bash
cd ~/repo/axscnt-agents && git pull
tools/install.sh                                # new dependencies (SKIP_APT=1 SKIP_DATA=1 to skip the rest)
tools/create_workspace.sh --update ~/opm-agents  # new agents, skills, MCP servers and CLAUDE.md
```

`--update` keeps `opmagent.toml`, the database and the data; restart Claude Code in the workspace afterwards. To
rebuild the venv (e.g. a new Python), remove `.venv` and run `tools/install.sh` again.

The same by hand:

```bash
# prerequisites
sudo apt update
sudo apt install -y python3-venv python3-pip git openjdk-17-jdk gdb software-properties-common
sudo apt-add-repository ppa:opm/ppa && sudo apt update
sudo apt install -y mpi-default-bin libopm-simulators-bin

# project
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,geo,opm]"     # opm + opm-simulators wheels: parser and simulator bindings
tools/fetch_open_data.sh            # SPE1, SPE9, Norne into ~/.local/share/opmagent/open-data
pytest -q                           # scratch databases; nothing to configure
OPM_TESTS_DIR=~/.local/share/opmagent/open-data/opm-tests pytest -q             # also the SPE1/SPE9 round trips
OPMAGENT_SLOW=1 OPM_TESTS_DIR=~/.local/share/opmagent/open-data/opm-tests pytest -q -k norne   # Norne (~20 min)

# the agents' workspace (MCP servers, agents, skills, config, database)
opmagent setup ~/opm-agents
cd ~/opm-agents
opmagent doctor                      # prerequisites and every agent
opmagent hello                       # builds the 4 demo models, waits for you
opmagent review approve-hello        # you approve (interactive: type 'approve')
opmagent hello                       # builds the HELLO deck, initialises it, parses it with OPM, runs it 5 years

# testing scenarios, from the repo, each in a fresh temporary workspace (see testing/)
NO_VIEWER=1 testing/spe9_loop.sh     # SPE9 reverse loop, about 30 s
NO_VIEWER=1 testing/geo_static_view.sh   # geo static view, about 25 s
NO_VIEWER=1 testing/init_view.sh     # initialisation view, about 45 s
```

Then start Claude Code in `~/opm-agents`: the agents (`.claude/agents/`), skills and MCP servers are picked up
from the workspace. Add the reviewer server from `reviewer-mcp.json` to **your own** client only.
Agent definitions limit each agent to its own servers via the `tools:` list; check `/agents` in Claude Code
after the first start.

Open test decks: `tools/fetch_open_data.sh` (SPE1, SPE9, Norne from opm-tests, ODbL). Volve must be
downloaded from Equinor's portal (Equinor Open Data Licence).

The OPM Flow binary from the OPM package archive (`libopm-simulators-bin`) is recommended: runs use `flow` when it's
on PATH, otherwise the `opm.simulators` Python bindings, but the geo static view and the deck initialisation view
need the `flow` binary.

## Studies

A study is one simulation question, worked through by all the agents in order:

```
deck builder starts it -> data manager loads the data into the database -> fluid, geo modelers -> res (on geo's grid), well modelers -> deck builder -> run agent -> results viewer
```

The database is the hub:
- **Tabular data.** The data manager converts tables (tops, wells, surveys, perforations, controls, production, the lab report) into the study data tables (`data_*`), each row linked to its source file.
- **File-native data.** LAS logs, SEG-Y, surfaces, grids, documents and images stay files, as they came, in `<study>/data/<category>/`, so Techlog, Petrel or ResInsight can open them. The database catalogs them (`data_files`; each LAS curve's unit, depth range and statistics in `data_log_curves`).
- **One way to read.** The modelers read both through one Python layer (`opmagent.data.store`). It checks every file against the checksum it was loaded with, and its checks combine both sides, for example tops against the porosity logs or the OWC against the SW logs.
- **Models.** The modelers analyse the data and build their models from it.
- **Review.** Review comments (`review_feedback`, with the modeler's answers) and approvals are stored next to the models.

```bash
opmagent study init FieldA_WF --requirement "Waterflood with 2 injectors, 20 years" --years 20 --report quarterly
# put the raw data in data/files/studies/FieldA_WF/inbox/ (lab report workbook, LAS, tops, well tables, Volve exports, geo.json)
opmagent data check FieldA_WF          # what each modeler will get, and what is missing (writes nothing)
opmagent data load FieldA_WF           # tables -> database; LAS, SEG-Y, docs ... filed in studies/FieldA_WF/data/ and catalogued
opmagent data show FieldA_WF [logs|files|wells|tops|history ...] [--well W]   # what is loaded; rows; log statistics; catalog
opmagent data verify FieldA_WF         # the filed originals are still the files that were loaded
opmagent study build geo FieldA_WF     # likewise fluid, res and well (after geo), then deck: from the database, with the study's names
opmagent review comment geo 12 "PERMX too high in layer 3" --item "PERMX L3"   # feedback the modeler answers
opmagent review feedback --all         # comments and answers

# the reverse process: a study that rebuilds an existing deck
opmagent study init SPE9_RB --requirement "rebuild SPE9"
opmagent data from-deck SPE9_RB ~/.local/share/opmagent/open-data/opm-tests/spe9/SPE9.DATA   # tables -> database, arrays -> files
opmagent study build geo SPE9_RB --complete   # likewise fluid, then res and well (after geo), then deck: the deck's data as given
opmagent study view SPE9_RB --open     # each model's QC report (PVT curves, layer maps, SCAL and contacts, wells), no simulation
opmagent study init SPE9_B --requirement "rebuild SPE9 from SPE9_RB's input"
opmagent data from-study SPE9_B SPE9_RB   # SPE9_B takes that input (tables copied, files copied and checked)
opmagent study compare SPE9_RB --deck-only   # same simulation deck? (parsed: grid, tables, arrays, schedule; no run)
opmagent study run SPE9_RB             # run the study's deck, store it, complete the run task
opmagent study compare SPE9_RB         # decks, then runs: static, initial state, every summary vector
opmagent deck compare A.DATA B.DATA    # any two decks
opmagent study task SPE9_RB data start|complete [--result JSON]   # the board from a terminal
opmagent study status FieldA_WF        # the board: tasks, open requests, what is next
opmagent study events FieldA_WF        # its history
opmagent results open --study FieldA_WF   # open the study's run in ResInsight (--geo <id>: a geo model's static view)
```

In Claude Code you don't type these: in your own session in the workspace, ask "start a study ..." and then "run
study FieldA_WF". The `study` skill coordinates: it reads the board and hands each agent its ready task. Agents
start and complete only their own task, through their data-server profile. A modeler that lacks data raises a
request to the data manager and is blocked until it is resolved. Completing a task checks its result (an approved
model version, a validated deck, a finished run) before the next tasks become ready. The board is in the
database (`studies`, `study_tasks`, `study_requests`, `study_events`), so DBeaver shows it too.

**Testing scenarios.** [testing/](testing/README.md) has scenarios anyone can follow to check the logic end to
end. The first is the SPE9 reverse loop: read SPE9 into study input and build the models (scenario 1), then
rebuild from that input, render the deck, run it and view it in ResInsight (scenario 2); the generated deck must be
the SPE9 deck again. `testing/spe9_loop.sh` runs both from a terminal. The geo static view scenario
(`testing/geo-static-view.md`, `testing/geo_static_view.sh`) checks the geo modeler's static model opens in
ResInsight and gives SPE9's and Norne's own INIT.

**Approval.** `[study] auto_approve = true` in `opmagent.toml` approves a model as soon as it passes its
validator, recorded in `approvals` as `auto (approval disabled)`; models with validator errors still stop.
With `false` (the default) you approve each model as before, and the study waits for it.

**ResInsight.** `tools/install_resinsight.sh` installs the latest release from github.com/OPM/ResInsight (its el8
build, which brings its own Qt) outside any workspace, where opmagent finds it without a setting (`opmagent doctor`
shows where). `tools/install.sh` runs it when ResInsight isn't found.

```bash
tools/install_resinsight.sh                       # ~/.local/opt/ResInsight, no sudo
SYSTEM=1 tools/install_resinsight.sh              # /opt/ResInsight and /usr/local/bin/resinsight, with sudo
tools/install_resinsight.sh ~/Downloads/ResInsight-2026.11.1_el8.tar.gz   # a release you downloaded
FORCE=1 tools/install_resinsight.sh               # replace (upgrade) an install; the old one is kept as .old
```

`VERSION=<tag>` installs a given release, `BUILD=ubuntu` the Ubuntu build (it needs `sudo apt install libomp5`).
Elsewhere, set `[viewer] resinsight_bin` to the launcher (`bin/resinsight` of the release: a symlink to it would not
work, the launcher finds its files from its own folder). On a Wayland desktop the viewer runs it with
`QT_QPA_PLATFORM=xcb` (the bundled Qt has only the X11 plugin).

## Fluid modeler

```bash
opmagent fluid template lab.xlsx            # empty template (add --example for the demo oil)
opmagent fluid build --input lab.xlsx --check            # list missing data
opmagent fluid build --input lab.xlsx --name FieldA_Oil  # build, QC report, submit for review
```

Exit code 0 means the model is waiting for review; the QC report path is in the JSON output. If Bo, Rs or
viscosity miss tolerance, nothing is written and the result says `needs_decision`.

### QC notebooks

The QC report is a Jupyter notebook. Every build runs the standard template
(`opmagent/notebooks/fluid_qc.ipynb`) headlessly in a fresh kernel of the opmagent environment and keeps the
results per version in `data/files/qc/fluid_<model>_v<N>/`:

| File | What it is |
|---|---|
| `payload.json` | what the build knew: lab data, simulated experiments, tuning checks, EOS spec |
| `fluid_qc.ipynb` | the executed notebook; open it in VS Code to re-run cells or change a plot |
| `fluid_qc.html` | the reviewer's copy, without code. It is the version's registered QC report, and an approval is tied to its sha256 |
| `qc_summary.json` | pass / warn / fail findings |
| `investigations/` | free-form notebooks |

The template reads the black-oil tables back from the database (read-only), so it checks the rows the deck
builder will write. It covers:
- **Lab data:** composition sum, DL monotonicity, the CCE break against Psat, and the characterised composition.
- **Phase envelope:** reservoir and separator conditions, and Psat from both the model and the lab.
- **Lab vs model:** CCE, DL (compared on the lab's own steps) and viscosity, with error tables.
- **What OPM needs from the tables:** saturated and undersaturated PVTO trends, an undersaturated branch on the
  top Rs, PVDG trends, pressure coverage, and RSVD within the Rs range.

Warn and fail findings don't block the build. They are listed for the reviewer and in the spoken summary. If a
cell fails, the version stays a draft and the error points at the notebook.

```bash
opmagent fluid investigate 12 --purpose "C7+ MW sensitivity on the DL match" --open   # starter notebook in VS Code
opmagent fluid run-notebook data/files/qc/fluid_FieldA_Oil_v3/investigations/01_C7_MW_sensitivity.ipynb
opmagent fluid qc 12          # re-run the standard QC for a draft (after return_to_draft), then resubmit
```

The agent has the same commands as MCP tools on the fluid profile: `create_investigation_notebook`,
`run_investigation_notebook` and `run_qc_notebook`. Notebooks only read and plot; changing the model always
takes a new build. Set `[qc] report = "html"` to go back to the static report.

The agent skill is in `skills/fluid-modeler/SKILL.md`. For Claude Code, copy or link `skills/*` into
`.claude/skills/` of the project you run the agents from.

## Geo modeler

```bash
opmagent geo build --tops tops.csv --logs *.las --spec geo.json --name FieldX_Geo   # grid, QC notebook, submit
opmagent geo investigate 7 --purpose "porosity at B in layer 2" --open    # starter notebook in VS Code
opmagent geo run-notebook data/files/qc/geo_FieldX_Geo_v1/investigations/01_porosity_at_B_in_layer_2.ipynb
opmagent geo qc 7             # re-run the standard QC for a draft (after return_to_draft), then resubmit
```

The geo QC report is a notebook too (`opmagent/notebooks/geo_qc.ipynb`), kept per version in
`data/files/qc/geo_<model>_v<N>/` with `payload.json`, `logs.csv` (the logs the build used), the executed
notebook, the HTML (the registered report) and `qc_summary.json`. It reads the cell arrays from their
registered files (sha256 checked) and the tables from the database. It covers:
- **Surfaces:** gridded surfaces against the well tops; zones pinched where surfaces were stopped from crossing.
- **Wells:** each well's model column against its logs and upscaled values; layers without log data; porosity
  predicted from the other wells (leave one well out).
- **Properties:** layer maps, sections through a well, distributions and poro-perm against the logs;
  PERMY/PERMZ against the spec; values held at the build's clip limits.
- **Volumes:** pore volume recomputed from the stored arrays; HCPV with the spec's OWC, for orientation only.

The geo model is the static model: grid, cell properties, PVTNUM and faults. SCAL, rock compressibility, contacts,
initial pressure and the region arrays are the res model's (next section).

**Static view in ResInsight.** Every geo build also makes a static view: the geo model as a deck (its GRID section
from the same renderer real decks use, plus placeholder fluid tables only so OPM can load it), made into EGRID + INIT
by an OPM Flow dry run in a few seconds. ResInsight then shows the grid and PORO, PERMX/Y/Z, NTG, PORV, DEPTH,
transmissibilities and the regions as OPM computes them, for drafts too, before any simulation. With a res model on
that geo version, its region arrays and SWATINIT are included; a geo model rebuilt from a deck includes that deck's
GRID passthrough (e.g. MULTREGT), so SPE9 and Norne match their own decks' INIT exactly.

```bash
opmagent results open --geo 7        # open geo version 7's static view (made if needed)
opmagent geo view 7 [--res 4 | --no-res] [--open]   # (re)make it; files in data/files/qc/geo_<model>_v<N>/static/
```

Findings don't block the build; they are listed for the reviewer. The geo and well profiles have the same notebook
tools as the fluid profile. Models from a deck keep their model view (`opmagent study view`).

## Res modeler

The res model is the reservoir engineer's half of the model, defined on one geo version's grid: SWOF/SGOF per
SATNUM, rock compressibility, contacts and initial pressure per EQLNUM (EQUIL), and the region arrays (SATNUM,
EQLNUM, FIPNUM, ROCKNUM; SWATINIT from imported decks). A deck takes four approved models, and the res version must
be defined on the deck's geo version.

```bash
opmagent res build --spec res.json --geo-version 7 --name FieldX_Res
opmagent study build res FieldA_WF     # in a study: after geo, from the study's contacts, SCAL workbook, geo.json rock/regions
opmagent res template scal.xlsx --example   # the SCAL workbook: Info, RockTypes (one row per SATNUM), SWOF, SGOF
opmagent res build --spec res.json --geo-version 7 --name FieldX_Res --scal scal.xlsx   # its rock types replace the spec's scal
```

```json
{"scal": {"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "nw": 2, "now": 2, "source": "analogue core"},
 "rock": {"p_ref": 200, "cr": 4.5e-5},
 "contacts": {"datum_depth": 2000, "datum_pressure": 200, "owc_depth": 2100},
 "regions": {"SATNUM": {"by": "zone", "values": {"Upper": 1, "Lower": 2}}}}
```

SCAL is Corey per rock type or measured tables (`"scal": [{"satnum": 1, "swof": [...], "sgof": [...]}, ...]`);
Corey parameters left out are listed as defaults in the assumptions. Regions go by geo zone, by layer range, or one
value.

**Res QC notebook** (`res_qc.ipynb`, run by every res build, like the others). It reads the geo version's static
view made with this version's regions, so it checks what Flow sees on any grid (pore volume, depth, SATNUM, EQLNUM,
FIPNUM per cell):
- SCAL per SATNUM: end points (Swc, Sorw, krw there, Sgc, Sorg), the mobile oil window, kro at connate water in
  SWOF against SGOF, SGOF within 1 - Swc, Pcow not rising with Sw; kr and Pc plots;
- regions: every SATNUM and EQLNUM value has its table and every table is used; cells and pore volume per region;
- contacts: datum within each region's cells, GOC above OWC, where each contact lies; rock compressibility range;
- aquifers: connected cells active, size against the reservoir's pore volume; a plan view of the connections.

```bash
opmagent res qc 12                                     # re-run it for a draft version
opmagent res investigate 12 --purpose "kr end points" --open
```

**SCAL workbook.** `opmagent res template` writes it: `Info` (unit system: METRIC with Pc in bar, or FIELD with Pc
in psi), `RockTypes` (one row per SATNUM: name, method `corey` or `tables`, source, and the Corey Swc, Sorw, Sorg,
Sgc, exponents and kr end points; blanks take the defaults and are noted), `SWOF` and `SGOF` (measured points per
SATNUM, for method `tables`). In a study's inbox the data manager recognises it by its RockTypes sheet, files it in
`data/scal/` and loads it into `data_scal`; the res modeler then takes the SCAL from it rather than from geo.json.
The reader stops on what would break the tables (no unit system, SATNUMs not 1..n, saturation not increasing, kr
outside 0-1, Swc + Sorw ≥ 1, a `tables` rock type without SWOF or SGOF rows) and warns on the rest (no source,
kr not starting or ending at 0).

**Analytical aquifers.** `"aquifers"` in the res spec (or geo.json in a study): Fetkovich (`ct`, `water_volume`,
`productivity_index`) or Carter-Tracy (`ct`, `permeability`, `porosity`, `inner_radius`, `thickness`, `angle`),
each with its connections to grid faces:

```json
"aquifers": [{"type": "fetkovich", "ct": 1e-4, "water_volume": 5e8, "productivity_index": 50,
              "connections": [{"face": "I-", "layers": "2-3"}], "source": "material balance"},
             {"type": "carter_tracy", "ct": 1e-4, "permeability": 200, "porosity": 0.25, "inner_radius": 2000,
              "thickness": 30, "connections": [{"face": "J+", "zone": "Lower", "i": "3-8"}]}]
```

A face alone connects that whole edge of the grid; `i`, `j`, `layers` or `zone` narrow it. The datum defaults to
the OWC and the initial pressure to equilibrium with the reservoir (both noted in the assumptions). The deck gets
`AQUDIMS`, `AQUFETP` / `AQUCT` (built-in influence table) and `AQUANCON`, and the summary `AAQR`, `AAQT`, `AAQP`
per aquifer. Aquifers in imported decks are still carried through unchanged
([design](docs/design/res-model.md)).

## Well modeler

```bash
opmagent well build --wells w.csv --surveys s.csv --perforations p.csv --history h.xlsx --history-format volve \
    --controls c.csv --name FieldX_Wells --geo-version 7      # the geo version: perforations placed in its grid
opmagent well investigate 9 --purpose "P1 oil volume above measured" --open
opmagent well qc 9            # re-run the standard QC for a draft, then resubmit
```

The well QC report is a notebook as well (`opmagent/notebooks/well_qc.ipynb`), kept in
`data/files/qc/well_<model>_v<N>/` with `history.csv`, the measured history as it came in. It covers:
- **Wells and trajectories:** heads against the surveys, wells inside the geo grid, impossible station spacing,
  dogleg severity; a plan view over the top layer's porosity.
- **Perforations:** open before the well first flows; with a geo version, each interval placed in the grid's
  cells the way the deck builder does: length outside the layers, tight cells, producers below the OWC or above
  the GOC; a section of each well through the zones.
- **History:** the volume the deck applies (each rate until the next event) against the measured volume, per well
  and phase. Rates are volume / producing days and `WCONHIST` applies them for the whole month, so downtime shows up
  here. Also missing months (the previous rate carries on), spikes and missing BHP.
- **Controls:** events before the start, prediction overlapping the history, targets far above the last rates;
  the schedule of every well.

## Deck builder and runs

```bash
opmagent deck build --spec spec.json   # spec: see skills/deck-builder/SKILL.md
opmagent run --deck FieldA_WF20        # runs the latest validated deck with that name, live progress on stderr
opmagent runs list                     # runs the run agent started
opmagent runs watch 12                 # follow run 12 live (default: the latest run); Ctrl-C stops watching only
opmagent review list                   # what's waiting for your review, with QC report paths
opmagent review approve fluid 12       # or: reject geo 7 --comment "OWC should be 2165 m" (interactive only)
opmagent import deck --input FIELD.DATA --name FieldX    # reverse a deck into models for review
opmagent import verify --name FieldX                    # after approval: rebuild, run both, compare
```

**Watching a run.** Progress comes from Flow's log: report step, simulated date, percent of simulated time,
elapsed time and an estimate of the time left, for example
`running, report step 188/247, 03-Jul-2004, 73.4% of simulated time, 4m00s elapsed, about 1m27s left`.
`opmagent run` and `opmagent import verify` show it while they run. `opmagent runs watch` follows a run the agent
started, or any run folder (`opmagent runs watch data/files/imports/NORNE/import-001/run_rebuilt`). It warns when the
log has had no new output for 2 minutes, and stops with the last log lines if Flow exits without finishing. The run
agent's `job_status` returns the same progress. The full log is `run.log` in the run folder (`tail -f` works too).

**Initialisation view.** Every deck build also initialises the deck: a copy with its SCHEDULE replaced by one
1e-4 day step without wells and its SUMMARY by the fluids-in-place vectors, run in OPM Flow. Report step 0 is the
initial state (PRESSURE, SWAT, SGAS, RS/RV) for ResInsight, and the build reports the fluids in place for the field
and per FIPNUM region, the average pressure, and checks against the res model's contacts. It is not a run.

```bash
opmagent deck init 2 [--open]                # (re)make it; prints in-place totals, checks, region totals
opmagent results open --deck 2 --init        # open it in ResInsight
```

## Agents' condition (doctor)

```bash
opmagent doctor              # prerequisites, then one line per agent
opmagent doctor -v           # every check, and each agent's tasks, requests, reviews and runs
opmagent doctor --deep       # also start each MCP server over stdio and list its tools (about 10 s)
opmagent doctor --json       # the agents report as JSON
```

Run it in the workspace (or pass `--workspace`). In Claude Code, the `opm-doctor` skill does the same: type
`/opm-doctor` or ask "how are the agents?", and it runs the doctor and tells you what needs attention and the fix.
(Claude Code's own `/doctor` checks Claude Code itself.) For each agent it checks:
- **installed:** `.claude/agents/<agent>.md` exists and names its MCP servers;
- **skill:** `.claude/skills/<skill>/SKILL.md` is the repo's current version (if not: `opmagent setup` again);
- **servers:** each server it uses is in `.mcp.json`, its command exists, and Claude Code has approved it in
  `.claude/settings.local.json` (a new server, such as `opm-data-res` after an upgrade, stays unused until
  approved); with `--deep`, each one starts and lists the tools its profile should have;
- **activity:** its last write through the data server, and failed writes in the last 24 hours (`audit_log`);
- **work:** study tasks waiting for it, open requests to it, models of its kind waiting for review, runs in
  progress (and runs whose Flow process is gone).

It also fails if the reviewer server is in the agents' `.mcp.json`. The exit code is 1 when an agent can't work as
set up; warnings (a skill out of date, a server not approved yet, failed writes) leave it at 0. Read only.

## Backup and clean

```bash
opmagent backup                  # ~/.local/share/opmagent/backups/<workspace>-<time>.tar.gz: opm.db, opmagent.toml, manifest.json
opmagent backup --files          # also the file store (data/files), without the views
opmagent clean                   # lists what can go, with sizes, and the size of each file-store folder; removes nothing
opmagent clean --yes             # removes it (--runs, --imports, --views: only those)
```

- **backup** copies the database with SQLite's online backup, so it is consistent while agents write. `--out <dir>`
  puts the archive elsewhere. To restore into the same workspace (the database keeps absolute paths into it): close
  Claude Code, `mv data/opm.db data/opm.db.before-restore`, then `tar -xzf <archive> -C /tmp/restore` and copy
  `opm.db` (and `files/` for a `--files` backup) back into `data/`.
- **clean** removes only what is safe to remove, inside the file store:
  - **runs**: the output folders of failed runs; the run rows stay, noted "files removed by opmagent clean";
  - **imports**: older import folders none of whose models are approved; the newest import of each deck is kept;
  - **views**: initialisation views and geo static views, made again when opened (`opmagent results open`).
- The workspace's `.claude/settings.json` makes the agents ask you before `opmagent clean`.

## Registering the data server

Each agent starts the data server with its own profile. Example MCP client config (Claude Code `.mcp.json` or Claude desktop):

```json
{
  "mcpServers": {
    "opm-data-fluid": {
      "command": "/path/to/axscnt-agents/.venv/bin/opmagent-data",
      "args": ["--profile", "fluid", "--config", "/path/to/opmagent.toml"]
    }
  }
}
```

The PVT engine server takes no profile: `opmagent-pvt --config /path/to/opmagent.toml`.

Profiles: `fluid`, `geo`, `res`, `well`, `import` (model writers; `res` also reads geo models), `data` (study data: inbox, deck or another study's
input), `deck`, `run`, `view` (read approved models only), `reviewer` (approve/reject).
Put the `reviewer` profile **only** in your own client, never in an agent's config. Approvals are recorded under
`$OPMAGENT_REVIEWER` (or your OS user name).

## Monitoring the agents (DBeaver)

Everything the agents do lands in the workspace database (`~/opm-agents/data/opm.db`), so any SQLite client shows it.
DBeaver Community Edition works well for browsing and querying:

```bash
sudo snap install dbeaver-ce --classic
```

Connect: **New Database Connection** > **SQLite** > path `~/opm-agents/data/opm.db` (accept the one-time driver
download). Under **Connection details**, tick **Read-only connection** before **Finish**.

Read-only matters. The database's triggers protect the approval gate (a decided model's status, the data of approved
or rejected versions, and the `approvals` and `audit_log` rows cannot be changed), but other tables such as `files`
(paths and checksums), `decks` and `runs` are not guarded. Approve and reject only with `opmagent review` or the
reviewer MCP server, never by editing tables.

Where to look:

| Table / view | Shows |
| --- | --- |
| `audit_log` | every data-server **write** (create version, write tables, submit, approve ...): agent (`profile`), tool, outcome (`ok` or the error code), time, arguments as a hash. Reads are not logged |
| `pending_review_fluid`, `_geo`, `_well` | models waiting for your review |
| `fluid_model_versions`, `geo_model_versions`, `well_model_versions` | every version and its status |
| `approvals` | approve / reject decisions, reviewer, comment |
| `decks`, `runs` | built decks and simulation runs started through the run agent |

Write activity per agent:

```sql
SELECT profile AS agent, COUNT(*) AS calls, SUM(outcome <> 'ok') AS failed, MAX(at) AS last_seen
FROM audit_log GROUP BY profile ORDER BY last_seen DESC;
```

DBeaver does not refresh by itself: press F5 on a table. Not in the database: calls to the PVT and simulation
servers, the Flow runs that `opmagent import verify` makes (their logs are under
`data/files/imports/<name>/import-NNN/run_*`), and the agents' own reasoning, which is in the Claude Code sessions
of the workspace.

## Layout

```
opmagent/
  core/         schema_base.sql, schema.sql (generated), db.py, config.py
  validators/   fluid, geo, res, well, cross-model rules
  servers/      data_server, pvt_server, sim_server (MCP)
  data/         study data: inbox load, checks, store (one read layer, checksums), deck and study input
  reports/      static QC reports ([qc] report = "html"), model views of deck data
  notebooks/    QC notebook templates (fluid, geo, res, well), qc_lib / geo_qc_lib / res_qc_lib / well_qc_lib, headless runner
  pvt/          NeqSim engine, demo oil
  fluid/        Excel lab report, fluid pipeline
  geo/          geo modeler (gridding, kriging)
  res/          res modeler (SCAL, rock, contacts, regions on a geo grid)
  well/         well modeler (ingest, Volve adapter)
  deck/         renderer, builder, importer, round trip, deck comparison
  sim/          runner
  hello/        default demo data
skills/         agent skills (SKILL.md per agent)
testing/        testing scenarios to follow by hand or by script (SPE9 reverse loop)
tools/          build_schema.py (regenerates schema.sql), gen_triggers.py, fetch_open_data.sh
docs/           links to the design docs; design/ for in-repo designs (res model)
tests/
```

Edit `opmagent/core/schema_base.sql`, then run `python tools/build_schema.py`.
