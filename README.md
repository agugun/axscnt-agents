# axscnt-agents

Agent system for OPM Flow reservoir simulation (black oil). Fluid, geo and well modeler agents build versioned models from your data; you approve each one; a deck builder turns approved models into validated OPM decks; a run agent runs them and stores results. A deck importer reverse-engineers existing decks with a round-trip check.

Design: see the architecture and user-guide docs (links in `docs/README.md`).

## Status

| Part | State |
| --- | --- |
| SQLite schema with human approval gate | done, tested |
| Config, `opmagent init-db`, `opmagent doctor` | done |
| Data MCP server (profiles, validators) | next |
| PVT engine MCP server (NeqSim) | planned |
| Fluid modeler skill + QC report | planned |
| Deck builder + `hello` | planned |
| Well modeler, geo modeler | planned |
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
python3 -m venv --system-site-packages .venv && source .venv/bin/activate   # system site: OPM python bindings
pip install -e ".[dev,geo]"
cp opmagent.example.toml opmagent.toml      # edit paths
opmagent init-db
opmagent doctor
pytest -q
```

## Layout

```
opmagent/
  core/         schema_base.sql, schema.sql (generated), db.py, config.py
  validators/   fluid, geo, well, cross-model rules
  servers/      data_server, pvt_server, sim_server (MCP)
  reports/      QC report builder
  hello/        default demo data
tools/          build_schema.py (regenerates schema.sql), gen_triggers.py
tests/
```

Edit `opmagent/core/schema_base.sql`, then run `python tools/build_schema.py`.
