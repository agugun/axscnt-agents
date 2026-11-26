#!/usr/bin/env bash
# Review loops: the engineer and the geo, res and well agents going back and forth through the review workbench
# (see testing/geo-review.md).
#
#   1. a small geo dataset (4 wells, LAS logs) and a geo build: v1
#   2. `opmagent geo review v1` serves the workbench
#   3. the geo loop, both sides (geo_review_check.py): a pinned comment, the waiting agent replies with a view, the
#      engineer answers, the agent proposes, the engineer accepts, the agent builds v2 from the proposal, the diff,
#      what-if previews (the agent's, then the engineer's own change, built as v3 and equal to its preview),
#      the end of the session
#   4. a res model on geo v1 and `opmagent res review`: a comment on the OWC, the res agent's preview (oil pore
#      volume moves to water, no Flow run), proposal, accept, `res build --from-proposal`, the diff, then the
#      engineer's own SCAL change over the base curves, built
#   5. a well model in geo v1 and `opmagent well review`: a comment on a perforation, the well agent's answer from
#      its cells, no previews (a well change is a data edit)
#
# Usage: testing/geo_review_loop.sh
#   WS=<dir>   workspace to create (default: a new temporary folder); must not exist yet
#   KEEP=1     leave the workbenches running at the end, to look at them in the browser (Ctrl-C stops them)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(dirname "$(readlink -f "$(command -v opmagent)")")/python"
CHECK="$PY $HERE/geo_review_check.py"
WS="${WS:-$(mktemp -d -t opmagent-geo-review-XXXX)/ws}"
SERVERS=()
RC=0

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
stop() { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
cleanup() { for p in "${SERVERS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

# workbench <kind> <version id>: start it, print its link into $URL
workbench() {
  local log="$WS/workbench_$1.log"
  opmagent "$1" review "$2" --no-open > "$log" 2>&1 &
  SERVERS+=($!)
  for _ in $(seq 1 60); do grep -q "token=" "$log" && break; sleep 0.5; done
  URL=$(grep -o 'http://127.0.0.1:[0-9]*/[^ ]*' "$log" | head -1)
  [ -n "$URL" ] || stop "the $1 workbench didn't start (see $log)"
  echo "  $URL"
}

# id <build result json>: its version id
id() { $PY -c "import json,sys; print(json.load(open(sys.argv[1]))['version_id'])" "$1"; }

[ -e "$WS" ] && stop "workspace $WS already exists: pick a new WS"
command -v opmagent >/dev/null || stop "opmagent not on PATH (activate the repo's venv)"

step "Workspace: $WS"
opmagent setup "$WS" >/dev/null
export OPMAGENT_CONFIG="$WS/opmagent.toml"

step "1. a small geo dataset and its build (v1)"
$PY "$HERE/geo_static_check.py" inputs "$WS/inputs"
opmagent geo build --tops "$WS/inputs/tops.csv" --logs "$WS/inputs/"*.las --spec "$WS/inputs/geo.json" \
  --name GEO_REVIEW_TEST > "$WS/geo_build.json" || stop "geo build failed (see $WS/geo_build.json)"
V1=$(id "$WS/geo_build.json")
echo "  geo version id $V1"

step "2. the geo workbench"
workbench geo "$V1"
GEO_URL=$URL

step "3. the geo loop, both sides"
$CHECK loop "$GEO_URL" "$V1" || RC=1

step "4. a res model on geo v1, and the res loop"
$CHECK inputs "$WS/inputs"
opmagent res build --spec "$WS/inputs/res.json" --name GEO_REVIEW_RES --geo-version "$V1" > "$WS/res_build.json" \
  || stop "res build failed (see $WS/res_build.json)"
R1=$(id "$WS/res_build.json")
echo "  res version id $R1"
workbench res "$R1"
RES_URL=$URL
$CHECK res "$RES_URL" "$R1" || RC=1

step "5. a well model in geo v1, and the well review"
opmagent well build --wells "$WS/inputs/wells.csv" --surveys "$WS/inputs/surveys.csv" \
  --perforations "$WS/inputs/perfs.csv" --history "$WS/inputs/history.csv" --controls "$WS/inputs/controls.csv" \
  --name GEO_REVIEW_WELLS --geo-version "$V1" > "$WS/well_build.json" || stop "well build failed (see $WS/well_build.json)"
W1=$(id "$WS/well_build.json")
echo "  well version id $W1"
workbench well "$W1"
WELL_URL=$URL
$CHECK well "$WELL_URL" "$W1" || RC=1

if [ "${KEEP:-}" = "1" ]; then
  step "The workbenches keep running (Ctrl-C stops them)"
  printf '  geo   %s\n  res   %s\n  well  %s\n' "$GEO_URL" "$RES_URL" "$WELL_URL"
  wait
fi
if [ $RC -eq 0 ]; then printf '\n\033[32mAll checks passed.\033[0m\n'; else printf '\n\033[31mSome checks failed.\033[0m\n'; fi
exit $RC
