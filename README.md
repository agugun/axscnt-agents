# axscnt-agents

Agent system for OPM Flow reservoir simulation (black oil). For each **study**, a data manager prepares the raw data; fluid, geo and well modeler agents build versioned models from it; you approve each one (or turn on auto-approve); a deck builder turns approved models into validated OPM decks; a run agent runs them and stores results; a results viewer opens them in ResInsight. A deck importer reverse-engineers existing decks with a round-trip check.

Version 0.2.0: see [CHANGELOG.md](CHANGELOG.md).

Design: see the architecture and user-guide docs (links in `docs/README.md`).

## Status

| Agent / part | How it runs | State |
| --- | --- | --- |
| Database + human approval gate (optional auto-approve) | SQLite, triggers | done, tested |
| Data server (profiles: fluid, geo, well, import, deck, run, data, view, reviewer) | MCP `opmagent-data` | done, tested |
| Studies: task board, requests between agents, coordinator skill | `opmagent study`, `study` skill | done, tested |
| Data manager (study inbox -> modeler input formats) | `opmagent data check / prepare` | done, tested |
| Results viewer (ResInsight) | `opmagent results open` | done, tested |
| Fluid modeler (NeqSim, Excel lab report) | MCP `opmagent-pvt` + `opmagent fluid build` | done, tested |
| Geo modeler (tops + LAS -> corner-point grid, kriging) | `opmagent geo build` | done, tested |
| Well modeler (surveys, perforations, history incl. Volve, controls) | `opmagent well build` | done, tested |
| Deck builder | `opmagent deck build` | done, tested |
| Run agent (runs, results, compare, VS Code debug) | MCP `opmagent-sim` | done, tested |
| Deck importer + round trip (SPE1, SPE9, Norne) | `opmagent import deck / verify` | done, tested |
| `hello` end-to-end demo | `opmagent hello` | done, tested |
| Local setup for Claude Code | `opmagent setup` | done |

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
opmagent hello                       # builds the 3 demo models, waits for you
opmagent review approve-hello        # you approve (interactive: type 'approve')
opmagent hello                       # builds the HELLO deck, parses it with OPM, runs it 5 years
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
deck builder starts it -> data manager loads the data into the database -> fluid, geo, well modelers -> deck builder -> run agent -> results viewer
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
opmagent study build geo FieldA_WF     # likewise fluid, well, then deck: from the database, with the study's names
opmagent review comment geo 12 "PERMX too high in layer 3" --item "PERMX L3"   # feedback the modeler answers
opmagent review feedback --all         # comments and answers
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

The agent skill is in `skills/fluid-modeler/SKILL.md`. For Claude Code, copy or link `skills/*` into
`.claude/skills/` of the project you run the agents from.

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
opmagent well build --wells w.csv --surveys s.csv --perforations p.csv --history h.xlsx --history-format volve --name FieldX_Wells
opmagent geo build --tops tops.csv --logs *.las --spec geo.json --name FieldX_Geo
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

Profiles: `fluid`, `geo`, `well`, `import` (writers), `deck`, `run` (read approved models only), `reviewer` (approve/reject).
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
  validators/   fluid, geo, well, cross-model rules
  servers/      data_server, pvt_server, sim_server (MCP)
  reports/      QC report builder
  pvt/          NeqSim engine, demo oil
  fluid/        Excel lab report, fluid pipeline
  geo/          geo modeler (gridding, kriging)
  well/         well modeler (ingest, Volve adapter)
  deck/         renderer, builder, importer, round trip
  sim/          runner
  hello/        default demo data
skills/         agent skills (SKILL.md per agent)
tools/          build_schema.py (regenerates schema.sql), gen_triggers.py
tests/
```

Edit `opmagent/core/schema_base.sql`, then run `python tools/build_schema.py`.
