#!/usr/bin/env bash
# Geo review loop: the engineer and the geo agent going back and forth on a geo model through the review
# workbench (see testing/geo-review.md).
#
#   1. a small geo dataset (4 wells, LAS logs) and a geo build: v1
#   2. `opmagent geo review v1` serves the workbench
#   3. the loop, both sides (geo_review_check.py): a pinned comment, the waiting agent replies with a view, the
#      engineer answers, the agent proposes, the engineer accepts, the agent builds v2 from the proposal, the diff,
#      the answer, the end of the session
#
# Usage: testing/geo_review_loop.sh
#   WS=<dir>   workspace to create (default: a new temporary folder); must not exist yet
#   KEEP=1     leave the workbench running at the end, to look at the loop in the browser (Ctrl-C stops it)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(dirname "$(readlink -f "$(command -v opmagent)")")/python"
WS="${WS:-$(mktemp -d -t opmagent-geo-review-XXXX)/ws}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
stop() { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

[ -e "$WS" ] && stop "workspace $WS already exists: pick a new WS"
command -v opmagent >/dev/null || stop "opmagent not on PATH (activate the repo's venv)"

step "Workspace: $WS"
opmagent setup "$WS" >/dev/null
export OPMAGENT_CONFIG="$WS/opmagent.toml"

step "1. a small geo dataset and its build (v1)"
$PY "$HERE/geo_static_check.py" inputs "$WS/inputs"
opmagent geo build --tops "$WS/inputs/tops.csv" --logs "$WS/inputs/"*.las --spec "$WS/inputs/geo.json" \
  --name GEO_REVIEW_TEST > "$WS/geo_build.json" || stop "geo build failed (see $WS/geo_build.json)"
V1=$($PY -c "import json,sys; print(json.load(open(sys.argv[1]))['version_id'])" "$WS/geo_build.json")
echo "  geo version id $V1"

step "2. the workbench"
opmagent geo review "$V1" --no-open > "$WS/workbench.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do grep -q "token=" "$WS/workbench.log" && break; sleep 0.5; done
URL=$(grep -o 'http://127.0.0.1:[0-9]*/[^ ]*' "$WS/workbench.log" | head -1)
[ -n "$URL" ] || stop "the workbench didn't start (see $WS/workbench.log)"
echo "  $URL"

step "3. the loop, both sides"
set +e
$PY "$HERE/geo_review_check.py" loop "$URL" "$V1"
RC=$?
set -e

if [ "${KEEP:-}" = "1" ]; then
  step "The workbench keeps running (Ctrl-C stops it)"
  wait $SERVER
fi
if [ $RC -eq 0 ]; then printf '\n\033[32mAll checks passed.\033[0m\n'; else printf '\n\033[31mSome checks failed.\033[0m\n'; fi
exit $RC
