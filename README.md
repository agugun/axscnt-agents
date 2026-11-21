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
| Res modeler (SCAL, rock, contacts and initial pressure, region arrays on a geo grid) | `opmagent res build` | done, tested; SCAL workbook, aquifers and QC notebook to come |
| Well modeler (surveys, perforations, history incl. Volve, controls, QC notebook) | `opmagent well build / qc / investigate` | done, tested |
| Deck builder | `opmagent deck build` | done, tested |
| Run agent (runs, results, compare, VS Code debug) | MCP `opmagent-sim` | done, tested |
| Deck importer + round trip (SPE1, SPE9, Norne) | `opmagent import deck / verify` | done, tested |
| `hello` end-to-end demo | `opmagent hello` | done, tested |
| Local setup for Claude Code | `opmagent setup` | done |
| Testing scenarios (SPE9 reverse loop) | `testing/spe9_loop.sh`, [testing/](testing/README.md) | done, run on SPE9 |

## Laptop setup (Ubuntu)

```bash
# prerequisites
sudo apt update
sudo apt install -y python3-venv python3-pip git openjdk-17-jdk gdb software-properties-common
sudo apt-add-repository ppa:opm/ppa && sudo apt update
sudo apt install -y mpi-default-bin libopm-simulators-bin python3-opm-simulators

# project
git clone https://github.com/agugun/axscnt-agents.git && cd axscnt-agents
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,geo,opm]"     # opm + opm-simulators wheels: parser and simulator bindings
cp opmagent.example.toml opmagent.toml      # edit paths
opmagent init-db
opmagent doctor
pytest -q
# with the open decks (tools/fetch_open_data.sh): SPE1/SPE9 round trips too; OPMAGENT_SLOW=1 adds Norne (~10 min)
OPM_TESTS_DIR=~/opmagent/open-data/opm-tests pytest -q
OPMAGENT_SLOW=1 OPM_TESTS_DIR=~/opmagent/open-data/opm-tests pytest -q -k norne

# create the agents workspace (MCP servers, agents, skills, config, database)
opmagent setup ~/opm-agents
cd ~/opm-agents

# end-to-end check
opmagent hello                       # builds the 4 demo models, waits for you
opmagent review approve-hello        # you approve (interactive: type 'approve')
opmagent hello                       # builds the HELLO deck, parses it with OPM, runs it 5 years

# testing scenario: the SPE9 reverse loop in a fresh temporary workspace (about 30 s; see testing/)
NO_VIEWER=1 testing/spe9_loop.sh     # from the repo; prints PASS SPE9 reverse loop
```

Then start Claude Code in `~/opm-agents`: the agents (`.claude/agents/`), skills and MCP servers are picked up
from the workspace. Add the reviewer server from `reviewer-mcp.json` to **your own** client only.
Agent definitions limit each agent to its own servers via the `tools:` list; check `/agents` in Claude Code
after the first start.

Open test decks: `tools/fetch_open_data.sh` (SPE1, SPE9, Norne from opm-tests, ODbL). Volve must be
downloaded from Equinor's portal (Equinor Open Data Licence).

The OPM Flow binary from the OPM package archive (`libopm-simulators-bin`) is optional: the runner uses
`flow` when it's on PATH, otherwise the `opm.simulators` Python bindings.

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
opmagent data from-deck SPE9_RB ~/opmagent/open-data/opm-tests/spe9/SPE9.DATA   # tables -> database, arrays -> files
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
opmagent results open --study FieldA_WF   # open the study's run in ResInsight
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
the SPE9 deck again. `testing/spe9_loop.sh` runs both from a terminal.

**Approval.** `[study] auto_approve = true` in `opmagent.toml` approves a model as soon as it passes its
validator, recorded in `approvals` as `auto (approval disabled)`; models with validator errors still stop.
With `false` (the default) you approve each model as before, and the study waits for it.

**ResInsight.** Set `[viewer] resinsight_bin` to the ResInsight launcher (`bin/resinsight` of the Linux
release). On a Wayland desktop the viewer runs it with `QT_QPA_PLATFORM=xcb` (the bundled Qt has only the X11
plugin).

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

Findings don't block the build; they are listed for the reviewer. The geo and well profiles have the same notebook
tools as the fluid profile. Models from a deck keep their model view (`opmagent study view`).

## Res modeler

The res model is the reservoir engineer's half of the model, defined on one geo version's grid: SWOF/SGOF per
SATNUM, rock compressibility, contacts and initial pressure per EQLNUM (EQUIL), and the region arrays (SATNUM,
EQLNUM, FIPNUM, ROCKNUM; SWATINIT from imported decks). A deck takes four approved models, and the res version must
be defined on the deck's geo version.

```bash
opmagent res build --spec res.json --geo-version 7 --name FieldX_Res
opmagent study build res FieldA_WF     # in a study: after geo, from the study's contacts and the geo.json scal/rock/regions
```

```json
{"scal": {"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "nw": 2, "now": 2, "source": "analogue core"},
 "rock": {"p_ref": 200, "cr": 4.5e-5},
 "contacts": {"datum_depth": 2000, "datum_pressure": 200, "owc_depth": 2100},
 "regions": {"SATNUM": {"by": "zone", "values": {"Upper": 1, "Lower": 2}}}}
```

SCAL is Corey per rock type or measured tables (`"scal": [{"satnum": 1, "swof": [...], "sgof": [...]}, ...]`);
Corey parameters left out are listed as defaults in the assumptions. Regions go by geo zone, by layer range, or one
value. The QC report is a static page for now (curves and end points, contacts, rock, regions). Next: a SCAL
workbook for the data manager, aquifers (build and render) and a res QC notebook
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
  notebooks/    QC notebook templates (fluid, geo, well), qc_lib / geo_qc_lib / well_qc_lib, headless runner
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
