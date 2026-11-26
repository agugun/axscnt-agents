# Testing scenarios

Scenarios anyone can follow to check that opmagent's logic works end to end, with the agents or from a terminal.
Each one says what to do, what you should see, and when it counts as passed.

| Scenario | What it proves | Time |
| --- | --- | --- |
| [SPE9 reverse loop](spe9-reverse-loop.md) | A deck read into study input gives the same models, and the same deck again. Running that deck gives the same results. | about 10 min by hand, under a minute scripted |
| [Geo static view](geo-static-view.md) | The geo modeler's static model opens in ResInsight before any simulation, and is the model as OPM sees it: SPE9's and Norne's static views give their own decks' INIT exactly. | about 10 min by hand, about 25 s scripted |
| [History matching on Norne](history-match-norne.md) | The history matcher on a real field: Norne imported, rebuilt and run, its misfit against its own 1997-2006 history, and a sensitivity on PERMX per formation and four faults. Reported, not a pass mark. | about 45 min scripted |
| [Review loops](geo-review.md) | The engineer and the geo, res and well agents go back and forth in the review workbench: pinned comments, the agents' replies with views, proposals accepted and built (geo and res), what-if previews (the engineer's own changes built and equal to their previews), diffs, sessions ended; well reviewed without proposals. | about 10 min by hand, about 50 s scripted |
| [Initialisation view](init-view.md) | The dynamic model (fluid, geo, res, well in a deck) initialises as its models say, and opens in ResInsight at time zero: SPE9's and Norne's rebuilt decks start exactly as their own decks do. | about 15 min by hand, about 45 s scripted |

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
   tools/fetch_open_data.sh          # into ~/.local/share/opmagent/open-data/opm-tests/
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
