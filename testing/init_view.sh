#!/usr/bin/env bash
# Initialisation view: the dynamic model (fluid + geo + res + well) at time zero in ResInsight
# (see testing/init-view.md).
#
#   part 1 (models): hello builds four models and a deck; the deck build initialises it: in place, checks
#   part 2 (decks):  SPE9 (and Norne) rebuilt through a study, four models and a deck; their initial state and fluids
#                    in place equal the source deck's own, initialised the same way
#   part 3 (viewer): each initialisation view opens in ResInsight
#
# Usage: testing/init_view.sh
#   OPM_TESTS=<dir>    opm-tests checkout (default: ~/.local/share/opmagent/open-data/opm-tests)
#   WS=<dir>           workspace to create (default: a new temporary folder); must not exist yet
#   RESINSIGHT=<path>  the ResInsight launcher (default: found on PATH or in /opt/ResInsight)
#   NO_VIEWER=1        don't start ResInsight (print what would open)
#   SKIP_NORNE=1       part 2 with SPE9 only (Norne adds a few minutes)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# the Python of the opmagent on PATH (the repo venv), so the check imports opmagent without activating it
PY="$(dirname "$(readlink -f "$(command -v opmagent)")")/python"
CHECK="$PY $HERE/init_view_check.py"
OPM_TESTS="${OPM_TESTS:-${XDG_DATA_HOME:-$HOME/.local/share}/opmagent/open-data/opm-tests}"
WS="${WS:-$(mktemp -d -t opmagent-init-view-XXXX)/ws}"
FAILS=0

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { printf '\033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
stop() { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
run_check() { if out=$("$@"); then echo "$out"; else echo "$out"; FAILS=$((FAILS + 1)); fi; }

[ -e "$WS" ] && stop "workspace $WS already exists: pick a new WS"
command -v opmagent >/dev/null || stop "opmagent not on PATH (activate the repo's venv)"
command -v flow >/dev/null || stop "OPM Flow not on PATH"
[ -f "$OPM_TESTS/spe9/SPE9.DATA" ] || stop "SPE9 not found under $OPM_TESTS (get it with tools/fetch_open_data.sh)"

step "Workspace: $WS"
opmagent setup "$WS" >/dev/null
sed -i 's/^auto_approve = false/auto_approve = true/' "$WS/opmagent.toml"
RESINSIGHT="${RESINSIGHT:-}"        # empty: opmagent finds it on PATH or in /opt/ResInsight
if [ -n "$RESINSIGHT" ] && [ -x "$RESINSIGHT" ]; then
  sed -i "s|^# resinsight_bin = .*|resinsight_bin = \"$RESINSIGHT\"|" "$WS/opmagent.toml"
fi
export OPMAGENT_CONFIG="$WS/opmagent.toml"
DECKS_TO_OPEN=()

# ------------------------------------------------------------------ part 1: from models
step "Part 1  hello: four demo models, the HELLO deck, its initialisation view"
opmagent hello | grep -E "^\[(pass|FAIL|wait)\]" || true
run_check $CHECK deck 1 --hc 0.8 --fpr 201 --fpr-tol 2
DECKS_TO_OPEN+=(1)

# ------------------------------------------------------------------ part 2: decks rebuilt through studies
LIST=("SPE9_IV:$OPM_TESTS/spe9/SPE9.DATA")
[ -n "${SKIP_NORNE:-}" ] || [ ! -f "$OPM_TESTS/norne/NORNE_ATW2013.DATA" ] || \
  LIST+=("NORNE_IV:$OPM_TESTS/norne/NORNE_ATW2013.DATA")
for item in "${LIST[@]}"; do
  S="${item%%:*}"; D="${item#*:}"
  step "Part 2  $S: $(basename "$D") rebuilt through a study (four models, then the deck)"
  opmagent study init "$S" --requirement "Initialisation view of $(basename "$D")" >/dev/null
  opmagent study task "$S" data start >/dev/null
  opmagent data from-deck "$S" "$D" >/dev/null
  opmagent study task "$S" data complete >/dev/null
  for k in fluid geo res well deck; do
    opmagent study build "$k" "$S" --complete | python3 -c 'import json,sys; print(" ", json.load(sys.stdin)["spoken_summary"][:400])'
  done
  step "Part 2  $S: the deck at time zero against the source deck's own"
  run_check $CHECK compare "$S" "$D"
  DECKS_TO_OPEN+=("$($CHECK deck-id "$S")")
done

# ------------------------------------------------------------------ part 3: ResInsight
step "Part 3  each initialisation view in ResInsight"
for d in "${DECKS_TO_OPEN[@]}"; do
  [ -n "$d" ] || continue
  if [ -n "${NO_VIEWER:-}" ]; then
    opmagent results open --deck "$d" --init --dry-run || fail "deck $d: nothing to open"
  else
    opmagent results open --deck "$d" --init && pass "deck $d opened in ResInsight" || fail "deck $d did not open"
  fi
done

step "Result"
if [ "$FAILS" -eq 0 ]; then
  pass "initialisation view (workspace $WS)"
else
  stop "initialisation view: $FAILS failed check(s) (workspace $WS)"
fi
