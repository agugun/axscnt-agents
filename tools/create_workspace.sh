#!/usr/bin/env bash
# Create an agents' workspace: config, database, file store, MCP servers (.mcp.json), agents and skills (.claude/),
# CLAUDE.md, and the reviewer server for your own client (reviewer-mcp.json). Then check it and run hello.
#
# Usage: tools/create_workspace.sh [workspace]          (default: ~/opm-agents; must not exist yet)
#   AUTO_APPROVE=1     approve models that pass their validator ([study] auto_approve = true); default: you approve
#   RESINSIGHT=<path>  the ResInsight launcher, if it isn't on PATH, in /opt/ResInsight or ~/.local/opt/ResInsight
#   SKIP_HELLO=1       don't run hello (the end-to-end check)
#   APPROVE_SERVERS=1  pre-approve the workspace's MCP servers for Claude Code (.claude/settings.local.json);
#                      otherwise Claude Code asks once per server at its first start
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WS="${1:-$HOME/opm-agents}"
WS="${WS/#\~/$HOME}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32mok\033[0m   %s\n' "$*"; }
die()  { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

# always this repo's environment, so the workspace's MCP servers run this repo's code
[ -x "$REPO/.venv/bin/opmagent" ] || die "opmagent isn't installed in $REPO/.venv: run $REPO/tools/install.sh first"
# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"
if [ -e "$WS" ] && [ -n "$(ls -A "$WS" 2>/dev/null)" ]; then
  die "$WS already exists and isn't empty: move it aside (e.g. mv $WS $WS.old) or pick another folder"
fi

step "Workspace: $WS"
opmagent setup "$WS"
TOML="$WS/opmagent.toml"
if [ -n "${AUTO_APPROVE:-}" ]; then
  sed -i 's/^auto_approve = false/auto_approve = true/' "$TOML"
  ok "auto-approve on: models that pass their validator are approved, recorded as 'auto (approval disabled)'"
fi
if [ -n "${RESINSIGHT:-}" ]; then
  [ -x "$RESINSIGHT" ] || die "RESINSIGHT=$RESINSIGHT isn't an executable"
  sed -i "s|^#\? *resinsight_bin = .*|resinsight_bin = \"$RESINSIGHT\"|" "$TOML"
  ok "ResInsight: $RESINSIGHT"
fi
if [ -n "${APPROVE_SERVERS:-}" ]; then
  python - "$WS" <<'PY'
import json, sys
from pathlib import Path
ws = Path(sys.argv[1])
servers = sorted(json.loads((ws / ".mcp.json").read_text())["mcpServers"])
p = ws / ".claude" / "settings.local.json"
s = json.loads(p.read_text()) if p.exists() else {}
s["enabledMcpjsonServers"] = sorted(set(s.get("enabledMcpjsonServers", [])) | set(servers))
p.write_text(json.dumps(s, indent=2) + "\n")
print(f"ok   MCP servers pre-approved for Claude Code: {', '.join(servers)}")
PY
fi
export OPMAGENT_CONFIG="$TOML"

step "Check (opmagent doctor)"
opmagent doctor || true
[ -n "${APPROVE_SERVERS:-}" ] || printf '     (agents warn "server not approved" until Claude Code approves them: it asks once per server at\n      its first start in %s; APPROVE_SERVERS=1 approves them now)\n' "$WS"

if [ -z "${SKIP_HELLO:-}" ]; then
  step "End-to-end check (opmagent hello)"
  cd "$WS"
  opmagent hello | grep -E "^\[(pass|FAIL|wait)\]" || true
  if grep -q '^auto_approve = true' "$TOML"; then
    ok "hello ran to the end (auto-approve)"
  else
    cat <<EOF
     The four hello models wait for your review. Approve them yourself (you type 'approve'), then run hello again:
       cd $WS && opmagent review approve-hello && opmagent hello
EOF
  fi
fi

cat <<EOF

Workspace ready: $WS
  cd $WS && claude                      # start Claude Code here: the agents, skills and MCP servers are picked up
  /agents                               # check: each agent sees only its own servers
  /opm-doctor                           # or: opmagent doctor (in the workspace)
Reviewer (approve / reject): add the server in $WS/reviewer-mcp.json to YOUR OWN client only, never to this workspace:
  claude mcp add-json opm-reviewer '<the opm-reviewer entry of reviewer-mcp.json>'   (in a session you use yourself)
EOF
