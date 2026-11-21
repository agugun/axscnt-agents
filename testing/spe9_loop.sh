#!/usr/bin/env bash
# SPE9 reverse loop, both scenarios, from a terminal (see testing/spe9-reverse-loop.md).
#
#   scenario 1 (SPE9_S1): SPE9.DATA -> study input -> fluid, geo, res, well models -> model views (no simulation)
#   scenario 2 (SPE9_S2): scenario 1's input -> models -> deck -> Flow run -> ResInsight
#   success:              the scenario 2 deck is the SPE9 deck again (decks compared, then runs compared)
#
# Usage: testing/spe9_loop.sh [path/to/SPE9.DATA]
#   WS=<dir>       workspace to create (default: a new temporary folder); must not exist yet
#   RESINSIGHT=<path>  the ResInsight launcher (default: ~/opm-agents/ResInsight/bin/resinsight if it exists)
#   NO_VIEWER=1    don't start ResInsight or the browser (print what would open)
set -euo pipefail

DECK="${1:-$HOME/opmagent/open-data/opm-tests/spe9/SPE9.DATA}"
WS="${WS:-$(mktemp -d -t opmagent-spe9-loop-XXXX)/ws}"
S1=SPE9_S1
S2=SPE9_S2

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { printf '\033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

[ -f "$DECK" ] || fail "deck not found: $DECK (get it with tools/fetch_open_data.sh)"
[ -e "$WS" ] && fail "workspace $WS already exists: pick a new WS"
command -v opmagent >/dev/null || fail "opmagent not on PATH (activate the repo's venv)"
command -v flow >/dev/null || fail "OPM Flow not on PATH"

step "Workspace: $WS"
opmagent setup "$WS" >/dev/null
# no human reviewer in an automated run: models that pass their validator are approved (recorded as such)
sed -i 's/^auto_approve = false/auto_approve = true/' "$WS/opmagent.toml"
RESINSIGHT="${RESINSIGHT:-$HOME/opm-agents/ResInsight/bin/resinsight}"
if [ -x "$RESINSIGHT" ]; then
  sed -i "s|^# resinsight_bin = .*|resinsight_bin = \"$RESINSIGHT\"|" "$WS/opmagent.toml"
fi
export OPMAGENT_CONFIG="$WS/opmagent.toml"
opmagent doctor || true

# ------------------------------------------------------------------ scenario 1
step "Scenario 1.1  study $S1"
opmagent study init "$S1" --requirement "Reverse SPE9: read the deck into study input and build the models"

step "Scenario 1.2  data manager reads the deck (check first, then load)"
opmagent study task "$S1" data start >/dev/null
opmagent data from-deck "$S1" "$DECK" --check
opmagent data from-deck "$S1" "$DECK"
opmagent data verify "$S1"
opmagent study task "$S1" data complete >/dev/null && pass "data task done: input loaded"

step "Scenario 1.3  modelers build their models from the input (geo before res and well)"
for k in fluid geo res well; do
  out=$(opmagent study build "$k" "$S1" --complete)
  echo "$out" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(" ", r["spoken_summary"]); print(" ", r.get("task"))'
done

step "Scenario 1.4  model views (no simulation)"
if [ -n "${NO_VIEWER:-}" ]; then opmagent study view "$S1"; else opmagent study view "$S1" --open; fi
pass "scenario 1: input and four models, each with its view"

# ------------------------------------------------------------------ scenario 2
step "Scenario 2.1  study $S2 takes scenario 1's input"
opmagent study init "$S2" --requirement "Rebuild SPE9 from scenario 1's input and run it"
opmagent study task "$S2" data start >/dev/null
opmagent data from-study "$S2" "$S1"
opmagent study task "$S2" data complete >/dev/null && pass "data task done: input taken from $S1"

step "Scenario 2.2  modelers build again, from that input"
for k in fluid geo res well; do
  opmagent study build "$k" "$S2" --complete | python3 -c 'import json,sys; r=json.load(sys.stdin); print(" ", r["spoken_summary"])'
done

step "Scenario 2.3  deck builder"
opmagent study build deck "$S2" --complete | python3 -c 'import json,sys; r=json.load(sys.stdin); print(" ", r.get("spoken_summary"), "\n ", r.get("path"))'

step "Success criterion 1: the generated deck is the SPE9 deck (no simulation)"
opmagent study compare "$S2" --deck-only && pass "same simulation deck" || fail "the decks differ (see the report above)"

step "Scenario 2.4  run agent: Flow"
opmagent study run "$S2"
opmagent study view "$S2"            # scenario 2's models: the same views as scenario 1's

step "Success criterion 2: the run reproduces SPE9's own run"
opmagent study compare "$S2" && pass "same results: static, initial state, every summary vector" \
  || fail "the runs differ (see the report above)"

step "Scenario 2.5  results viewer: ResInsight"
if [ -n "${NO_VIEWER:-}" ]; then opmagent results open --study "$S2" --dry-run; else opmagent results open --study "$S2"; fi

step "Done"
echo "  workspace  $WS"
echo "  reports    $WS/data/files/studies/$S2/deck/compare_with_source_deck.json"
echo "             $WS/data/files/studies/$S2/runs/compare_with_source.json"
pass "SPE9 reverse loop"
