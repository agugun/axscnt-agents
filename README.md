# axscnt-agents

Agent system for OPM Flow reservoir simulation (black oil). Fluid, geo and well modeler agents build versioned models from your data; you approve each one; a deck builder turns approved models into validated OPM decks; a run agent runs them and stores results. A deck importer reverse-engineers existing decks with a round-trip check.

Design: see the architecture and user-guide docs (links in `docs/README.md`).

## Status

| Part | State |
| --- | --- |
| SQLite schema with human approval gate | done, tested |
| Config, `opmagent init-db`, `opmagent doctor` | done |
| Data MCP server (profiles, validators, reviewer tools) | done, tested |
| PVT engine MCP server (NeqSim): characterise, CCE/DL/separator/viscosity, Psat tuning, black-oil tables | done, tested |
| Fluid modeler: Excel template/reader, pipeline, QC report, agent skill | done, tested |
| Deck builder, `hello`, run (flow binary or opm.simulators) | done, tested |
| Human review CLI (`opmagent review`) | done |
| Well modeler, geo modeler (real data) | next |
| Run agent (+ debug), deck importer | planned |

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

# end-to-end check
opmagent hello                       # builds the 3 demo models, waits for you
opmagent review approve-hello        # you approve (shows what it will approve, asks y/N)
opmagent hello                       # builds the HELLO deck, parses it with OPM, runs it 5 years
```

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
opmagent review approve fluid 12       # or: reject geo 7 --comment "OWC should be 2165 m"
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
  hello/        default demo data
skills/         agent skills (SKILL.md per agent)
tools/          build_schema.py (regenerates schema.sql), gen_triggers.py
tests/
```

Edit `opmagent/core/schema_base.sql`, then run `python tools/build_schema.py`.
