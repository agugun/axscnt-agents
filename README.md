# axscnt-agents

Agent system for OPM Flow reservoir simulation (black oil). Fluid, geo and well modeler agents build versioned models from your data; you approve each one; a deck builder turns approved models into validated OPM decks; a run agent runs them and stores results. A deck importer reverse-engineers existing decks with a round-trip check.

Design: see the architecture and user-guide docs (links in `docs/README.md`).

## Status

| Agent / part | How it runs | State |
| --- | --- | --- |
| Database + human approval gate | SQLite, triggers | done, tested |
| Data server (profiles: fluid, geo, well, import, deck, run, reviewer) | MCP `opmagent-data` | done, tested |
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
opmagent run --deck FieldA_WF20        # runs the latest validated deck with that name
opmagent review list                   # what's waiting for your review, with QC report paths
opmagent review approve fluid 12       # or: reject geo 7 --comment "OWC should be 2165 m" (interactive only)
opmagent import deck --input FIELD.DATA --name FieldX    # reverse a deck into models for review
opmagent import verify --name FieldX                    # after approval: rebuild, run both, compare
opmagent well build --wells w.csv --surveys s.csv --perforations p.csv --history h.xlsx --history-format volve --name FieldX_Wells
opmagent geo build --tops tops.csv --logs *.las --spec geo.json --name FieldX_Geo
```

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
