#!/usr/bin/env bash
# Download open test decks (SPE1, SPE9, Norne) from OPM's opm-tests repository.
# Licence: Open Database License (ODbL) for the datasets; see the repository README.
# Data stays outside this repo: default target ~/.local/share/opmagent/open-data
set -euo pipefail
DEST="${1:-${XDG_DATA_HOME:-$HOME/.local/share}/opmagent/open-data}"
mkdir -p "$DEST"
if [ ! -d "$DEST/opm-tests/.git" ]; then
  git clone --depth 1 --filter=blob:none --sparse https://github.com/OPM/opm-tests.git "$DEST/opm-tests"
fi
cd "$DEST/opm-tests"
git sparse-checkout set spe1 spe9 norne
echo "Decks:"
echo "  $DEST/opm-tests/spe1/SPE1CASE1.DATA"
echo "  $DEST/opm-tests/spe9/SPE9.DATA"
echo "  $DEST/opm-tests/norne/NORNE_ATW2013.DATA"
echo "Volve is not downloadable automatically: get it from Equinor's data portal (Equinor Open Data Licence)."
