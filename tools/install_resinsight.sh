#!/usr/bin/env bash
# Install ResInsight (the results viewer) outside any workspace, where opmagent finds it without a setting.
#
# Usage: tools/install_resinsight.sh [archive]
#   archive            a ResInsight Linux release you downloaded (.zip or .tar.gz); default: the latest release of
#                      github.com/OPM/ResInsight, its el8 build (it brings its own Qt and needs nothing else on Ubuntu)
#   BUILD=ubuntu       the Ubuntu build instead (needs the LLVM OpenMP runtime: sudo apt install libomp5)
#   DEST=<dir>         where to install (default: ~/.local/opt/ResInsight; /opt/ResInsight with SYSTEM=1)
#   SYSTEM=1           install in /opt/ResInsight with sudo, and add /usr/local/bin/resinsight
#   VERSION=<tag>      a release tag instead of the latest (e.g. v2026.11.1)
#   FORCE=1            replace an existing install (it is moved to <dest>.old first, not deleted)
set -euo pipefail

if [ -n "${SYSTEM:-}" ]; then DEST="${DEST:-/opt/ResInsight}"; SUDO=sudo; else DEST="${DEST:-$HOME/.local/opt/ResInsight}"; SUDO=""; fi
ARCHIVE="${1:-}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32mok\033[0m   %s\n' "$*"; }
die()  { printf '\033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

if [ -e "$DEST" ] && [ -z "${FORCE:-}" ]; then
  ok "ResInsight already installed: $DEST (FORCE=1 replaces it)"
  exit 0
fi
TMP="$(mktemp -d -t opmagent-resinsight-XXXX)"
trap 'rm -rf "$TMP"' EXIT

if [ -z "$ARCHIVE" ]; then
  step "Latest ResInsight release (github.com/OPM/ResInsight)"
  API="https://api.github.com/repos/OPM/ResInsight/releases/${VERSION:+tags/$VERSION}"
  [ -n "${VERSION:-}" ] || API="${API}latest"
  URLS="$(curl -fsSL "$API" | grep -o '"browser_download_url": *"[^"]*"' | sed 's/.*"\(http[^"]*\)"/\1/')" \
    || die "could not read $API (offline? download a release yourself and pass it as the argument)"
  if [ "${BUILD:-el8}" = ubuntu ]; then URL="$(grep -i 'ubuntu' <<<"$URLS" | head -1 || true)"
  else URL="$(grep -E '_el[0-9]+\.tar\.gz$' <<<"$URLS" | head -1 || true)"; fi
  [ -n "$URL" ] || die "no Linux build in $API"
  ARCHIVE="$TMP/$(basename "$URL")"
  printf '     %s\n' "$URL"
  curl -fL --progress-bar -o "$ARCHIVE" "$URL"
fi
[ -f "$ARCHIVE" ] || die "$ARCHIVE not found"

step "Unpack $(basename "$ARCHIVE")"
mkdir "$TMP/x"
case "$ARCHIVE" in
  *.zip)            unzip -q "$ARCHIVE" -d "$TMP/x" ;;
  *.tar.gz|*.tgz)   tar -xzf "$ARCHIVE" -C "$TMP/x" ;;
  *)                die "$ARCHIVE: expected a .zip or .tar.gz" ;;
esac
EXE="$(find "$TMP/x" -path '*/bin/ResInsight' -type f | head -1)"
[ -n "$EXE" ] || die "no bin/ResInsight in $ARCHIVE"
TOP="$(dirname "$(dirname "$EXE")")"
chmod +x "$TOP/bin/ResInsight" "$TOP/bin/resinsight" 2>/dev/null || true

step "Install in $DEST"
if [ -e "$DEST" ]; then
  $SUDO rm -rf "$DEST.old"
  $SUDO mv "$DEST" "$DEST.old"
  ok "the earlier install moved to $DEST.old"
fi
$SUDO mkdir -p "$(dirname "$DEST")"
$SUDO mv "$TOP" "$DEST"
LAUNCHER="$DEST/bin/resinsight"
[ -x "$LAUNCHER" ] || LAUNCHER="$DEST/bin/ResInsight"
ok "ResInsight: $LAUNCHER"
if [ -n "${SYSTEM:-}" ]; then
  printf '#!/bin/sh\nexec %s "$@"\n' "$LAUNCHER" | sudo tee /usr/local/bin/resinsight >/dev/null
  sudo chmod 755 /usr/local/bin/resinsight
  ok "/usr/local/bin/resinsight starts it"
fi
missing="$(LD_LIBRARY_PATH="$DEST/lib:$DEST/bin" ldd "$DEST/bin/ResInsight" 2>/dev/null | grep 'not found' || true)"
if [ -n "$missing" ]; then
  printf '\033[33mwarn\033[0m libraries missing:\n%s\n' "$(sort -u <<<"$missing")"
  grep -q libomp <<<"$missing" && printf '     libomp: sudo apt install libomp5 (or use the el8 build: FORCE=1 without BUILD=ubuntu)\n'
fi
printf '\nopmagent finds it there (opmagent doctor shows it); open a result with: opmagent results open ...\n'
