#!/usr/bin/env bash
# Geo static view: the geo modeler's static model as EGRID + INIT for ResInsight (see testing/geo-static-view.md).
#
#   part 1 (raw data): tops + LAS + geo.json -> geo build -> static view; its INIT holds the model's own arrays
#   part 2 (decks):    SPE9 (and Norne) read into a study -> geo and res builds -> static view; its INIT is the
#                      deck's own INIT (pore volumes, permeabilities, transmissibilities, regions)
#   part 3 (viewer):   each static view opens in ResInsight
#
# Usage: testing/geo_static_view.sh
#   OPM_TESTS=<dir>    opm-tests checkout (default: ~/.local/share/opmagent/open-data/opm-tests)
#   WS=<dir>           workspace to create (default: a new temporary folder); must not exist yet
#   RESINSIGHT=<path>  the ResInsight launcher (default: found on PATH or in /opt/ResInsight)
#   NO_VIEWER=1        don't start ResInsight (print what would open)
#   SKIP_NORNE=1       part 2 with SPE9 only (Norne adds about a minute)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK="python3 $HERE/geo_static_check.py"
OPM_TESTS="${OPM_TESTS:-${XDG_DATA_HOME:-$HOME/.local/share}/opmagent/open-data/opm-tests}"
WS="${WS:-$(mktemp -d -t opmagent-geo-view-XXXX)/ws}"
FAILS=0

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { printf '\033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
stop() { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
check() { if "$@"; then :; else FAILS=$((FAILS + 1)); fi; }

[ -e "$WS" ] && stop "workspace $WS already exists: pick a new WS"
command -v opmagent >/dev/null || stop "opmagent not on PATH (activate the repo's venv)"
command -v flow >/dev/null || stop "OPM Flow not on PATH (the static view is an OPM Flow dry run)"
[ -f "$OPM_TESTS/spe9/SPE9.DATA" ] || stop "SPE9 not found under $OPM_TESTS (get it with tools/fetch_open_data.sh)"

step "Workspace: $WS"
opmagent setup "$WS" >/dev/null
sed -i 's/^auto_approve = false/auto_approve = true/' "$WS/opmagent.toml"
RESINSIGHT="${RESINSIGHT:-}"        # empty: opmagent finds it on PATH or in /opt/ResInsight
if [ -n "$RESINSIGHT" ] && [ -x "$RESINSIGHT" ]; then
  sed -i "s|^# resinsight_bin = .*|resinsight_bin = \"$RESINSIGHT\"|" "$WS/opmagent.toml"
fi
export OPMAGENT_CONFIG="$WS/opmagent.toml"
VIEWS=()

# ------------------------------------------------------------------ part 1: from raw data
step "Part 1.1  a small geo dataset: 4 wells, 3 surfaces, LAS logs, geo.json"
$CHECK inputs "$WS/inputs"

step "Part 1.2  geo modeler builds the model (the static view is made with it)"
opmagent geo build --tops "$WS/inputs/tops.csv" --logs "$WS/inputs/"*.las --spec "$WS/inputs/geo.json" \
  --name GEO_VIEW_TEST > "$WS/geo_build.json" || true
grep -o '"spoken_summary": "[^"]*"' "$WS/geo_build.json" | sed 's/"spoken_summary": /  /'
if grep -q '"static_view": "' "$WS/geo_build.json"; then pass "geo build reports its static view"
else fail "geo build has no static view (see $WS/geo_build.json: static_view_error)"; fi

step "Part 1.3  the static view holds the model"
if out=$($CHECK model GEO_VIEW_TEST); then echo "$out"; else echo "$out"; FAILS=$((FAILS + 1)); fi
VIEWS+=("$(echo "$out" | sed -n 's/^geo version \([0-9]*\):.*/\1/p')")

# ------------------------------------------------------------------ part 2: from decks
DECKS=("SPE9_GV:$OPM_TESTS/spe9/SPE9.DATA")
[ -n "${SKIP_NORNE:-}" ] || [ ! -f "$OPM_TESTS/norne/NORNE_ATW2013.DATA" ] || \
  DECKS+=("NORNE_GV:$OPM_TESTS/norne/NORNE_ATW2013.DATA")
for item in "${DECKS[@]}"; do
  S="${item%%:*}"; D="${item#*:}"
  step "Part 2  $S: $(basename "$D") read into a study, geo and res built"
  opmagent study init "$S" --requirement "Static view of $(basename "$D")" >/dev/null
  opmagent study task "$S" data start >/dev/null
  opmagent data from-deck "$S" "$D" >/dev/null
  opmagent study task "$S" data complete >/dev/null
  for k in geo res; do
    opmagent study build "$k" "$S" --complete | python3 -c 'import json,sys; print(" ", json.load(sys.stdin)["spoken_summary"])'
  done
  step "Part 2  $S: the static view against the deck's own INIT"
  if out=$($CHECK compare "$S" "$D"); then echo "$out"; else echo "$out"; FAILS=$((FAILS + 1)); fi
  VIEWS+=("$($CHECK geo-id "$S")")
done

# ------------------------------------------------------------------ part 3: ResInsight
step "Part 3  each static view in ResInsight"
for v in "${VIEWS[@]}"; do
  [ -n "$v" ] || continue
  if [ -n "${NO_VIEWER:-}" ]; then
    opmagent results open --geo "$v" --dry-run || fail "geo version $v: nothing to open"
  else
    opmagent results open --geo "$v" && pass "geo version $v opened in ResInsight" || fail "geo version $v did not open"
  fi
done

step "Result"
if [ "$FAILS" -eq 0 ]; then
  pass "geo static view (workspace $WS)"
else
  stop "geo static view: $FAILS failed check(s) (workspace $WS)"
fi
