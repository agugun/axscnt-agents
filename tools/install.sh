#!/usr/bin/env bash
# Install opmagent on Ubuntu: system packages, OPM Flow, the repo's virtual environment, the open test decks.
# Run from a clone of this repo (or anywhere: it works on the repo it lives in). Safe to run again.
#
# Usage: tools/install.sh
#   SKIP_APT=1        skip the system packages and OPM Flow (no sudo); the rest still runs
#   SKIP_DATA=1       don't fetch the open decks (SPE1, SPE9, Norne) into ~/.local/share/opmagent/open-data
#   RUN_TESTS=1       run the test suite at the end (about 7 min; with the open decks, also SPE1/SPE9 round trips)
#   PYTHON=<python>   the interpreter for the virtual environment (default: the system's /usr/bin/python3, else
#                     python3 on PATH; needs 3.11 or newer). An existing .venv is kept: remove it to rebuild.
#   NO_LINK=1         don't link opmagent into ~/.local/bin (then activate .venv to use it)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# the system Python by default: a venv made from a conda or pyenv Python breaks when that Python is updated or removed
if [ -z "${PYTHON:-}" ]; then
  if [ -x /usr/bin/python3 ]; then PYTHON=/usr/bin/python3; else PYTHON=python3; fi
fi
OPEN_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/opmagent/open-data"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '\033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

# ------------------------------------------------------------------ system packages
if [ -z "${SKIP_APT:-}" ]; then
  step "System packages (sudo): Python venv, git, Java 17 (NeqSim), gdb"
  sudo apt-get update
  sudo apt-get install -y python3-venv python3-pip git openjdk-17-jdk gdb software-properties-common
  step "OPM Flow (release build) from the OPM packages"
  if command -v flow >/dev/null; then
    ok "flow already installed: $(command -v flow) ($(flow --version 2>/dev/null | head -1))"
  else
    sudo apt-add-repository -y ppa:opm/ppa
    sudo apt-get update
    sudo apt-get install -y mpi-default-bin libopm-simulators-bin
  fi
else
  step "System packages: skipped (SKIP_APT=1)"
fi
command -v java >/dev/null && ok "java: $(java -version 2>&1 | head -1)" || warn "java not found: the fluid modeler (NeqSim) needs Java 17+"
command -v flow >/dev/null && ok "flow: $(command -v flow)" || warn "flow not found: runs fall back to the opm.simulators Python bindings; static and initialisation views need flow"

# ------------------------------------------------------------------ the repo's virtual environment
step "Virtual environment: $REPO/.venv"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "$PYTHON is $("$PYTHON" --version 2>&1); opmagent needs Python 3.11 or newer (set PYTHON=...)"
[ -x "$REPO/.venv/bin/python" ] || "$PYTHON" -m venv "$REPO/.venv"
# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"
python -m pip install -q --upgrade pip
python -m pip install -q -e "$REPO[dev,geo,opm]"     # neqsim, mcp, lasio, gstools, opm, opm-simulators, nbclient ...
ok "opmagent $(opmagent --version 2>&1 | awk '{print $NF}') in $REPO/.venv ($(python --version), from $(sed -n 's/^home = //p' "$REPO/.venv/pyvenv.cfg"))"

# ------------------------------------------------------------------ opmagent on PATH
if [ -z "${NO_LINK:-}" ]; then
  BIN="$HOME/.local/bin"
  mkdir -p "$BIN"
  if [ -e "$BIN/opmagent" ] && [ ! -L "$BIN/opmagent" ]; then
    warn "$BIN/opmagent exists and isn't a link: left as it is"
  else
    ln -sfn "$REPO/.venv/bin/opmagent" "$BIN/opmagent"
    ok "opmagent on PATH: $BIN/opmagent -> $REPO/.venv/bin/opmagent"
    case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN isn't on PATH: add it (e.g. in ~/.profile), or activate .venv" ;; esac
  fi
fi

# ------------------------------------------------------------------ open test decks
if [ -z "${SKIP_DATA:-}" ]; then
  step "Open test decks (SPE1, SPE9, Norne from OPM's opm-tests, ODbL) into $OPEN_DATA"
  "$REPO/tools/fetch_open_data.sh" "$OPEN_DATA" | tail -4
else
  step "Open test decks: skipped (SKIP_DATA=1)"
fi

# ------------------------------------------------------------------ check
step "Check"
for m in neqsim opm.io lasio gstools nbclient mcp; do
  python -c "import $m" 2>/dev/null && ok "python: $m" || warn "python: $m does not import"
done
if [ -n "${RUN_TESTS:-}" ]; then
  step "Test suite"
  ( cd "$REPO" && OPM_TESTS_DIR="$OPEN_DATA/opm-tests" pytest -q )
fi

cat <<EOF

Installed. Next:
  $REPO/tools/create_workspace.sh ~/opm-agents        # the agents' workspace (a new one)
  $REPO/tools/create_workspace.sh --update ~/opm-agents   # or: refresh an existing one after an upgrade
EOF
