# Testing scenarios

Scenarios anyone can follow to check that opmagent's logic works end to end, with the agents or from a terminal.
Each one says what to do, what you should see, and when it counts as passed.

| Scenario | What it proves | Time |
| --- | --- | --- |
| [SPE9 reverse loop](spe9-reverse-loop.md) | A deck read into study input gives the same models, and the same deck again. Running that deck gives the same results. | about 10 min by hand, under a minute scripted |

The automated test suite (`pytest`) covers the same logic on small decks. These scenarios run it on a real deck,
through the steps an engineer takes, and show the results you can look at (QC reports, ResInsight).

## Before any scenario

1. **opmagent installed** from this repo, in a virtual environment:
   ```bash
   python -m venv .venv && source .venv/bin/activate && pip install -e .
   ```
2. **OPM Flow** on `PATH` (`flow --version`). `opmagent doctor` checks the rest.
3. **Open decks** (SPE1, SPE9, Norne from OPM's opm-tests):
   ```bash
   tools/fetch_open_data.sh          # into ~/opmagent/open-data/opm-tests/
   ```
4. **ResInsight**, to look at runs. Optional; the scenarios say what to do without it.
5. **A workspace just for testing**, so the test studies stay out of your real one:
   ```bash
   opmagent setup ~/opmagent-testing
   export OPMAGENT_CONFIG=~/opmagent-testing/opmagent.toml
   ```
   In `~/opmagent-testing/opmagent.toml`, set the ResInsight path under `[viewer]`. If you won't review each model
   yourself, also set `[study] auto_approve = true`.

## Recording a result

When you run a scenario, note:
- the date
- the commit (`git rev-parse --short HEAD`)
- PASS or FAIL for each success criterion
- the report files it names

A FAIL report should include the differences the compare printed and the JSON report.
