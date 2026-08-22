#!/usr/bin/env bash
# Re-vendor the gate scripts from a source checkout and re-pin vendor.lock.
#
# Contributors do not need to run this. The vendored copies are committed, and
# verify-gates.sh checks them by hash on every clone and in CI. This script
# exists so that taking an upstream change is a command anyone can run against
# their own copy of the source tree, instead of a path that resolves on one
# machine only.
#
# Usage:
#   ./.githooks/revendor.sh --source <dir>
#   ENGRAM_GATE_SOURCE_DIR=<dir> ./.githooks/revendor.sh
#
# <dir> is a directory holding the upstream gate scripts (see
# .githooks/README.md for what upstream is and how to propose a change to it).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd -P)"
DEST="$ROOT/.githooks"

# The vendored set. verify-gates.sh reads the same list out of vendor.lock, so
# adding a file here and re-running is what puts it under the hash check.
VENDORED="pii-scan.sh pii-patterns.conf planning-vocab-scan.sh planning-vocab-patterns.conf planning-vocab-ratchet.sh"

die() { echo "revendor: $*" >&2; exit 2; }

SOURCE="${ENGRAM_GATE_SOURCE_DIR:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --source) shift; [ $# -gt 0 ] || die "--source needs a directory"; SOURCE="$1" ;;
    --source=*) SOURCE="${1#--source=}" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) die "unrecognised option: $1 (expected --source <dir>)" ;;
  esac
  shift
done

[ -n "$SOURCE" ] || die "no source directory. Pass --source <dir> or set ENGRAM_GATE_SOURCE_DIR."
[ -d "$SOURCE" ] || die "source directory does not exist: $SOURCE"

# Check the whole set before copying any of it. A half-applied re-vendor leaves
# the tree in a state where the hash check fails and it is not obvious why.
missing=""
for name in $VENDORED; do
  [ -f "$SOURCE/$name" ] || missing="$missing $name"
done
if [ -n "$missing" ]; then
  die "source is missing:$missing (is $SOURCE the gate-script directory?)"
fi

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else sha256sum "$1" | cut -d' ' -f1; fi
}

for name in $VENDORED; do
  cp "$SOURCE/$name" "$DEST/$name" || die "copy failed: $name"
done
chmod +x "$DEST"/*.sh

lock="$DEST/vendor.lock"
{
  echo "# sha256  path  (hash is the identity; path is metadata)"
  echo "# Each line carries pii-allow: the 40-hex GPG-fingerprint pattern also"
  echo "# matches the first 40 chars of any sha256."
  for name in $VENDORED; do
    echo "$(sha256_of "$DEST/$name")  .githooks/$name  # pii-allow: sha256"
  done
} > "$lock.tmp" || die "could not write $lock.tmp"
mv "$lock.tmp" "$lock"

echo "revendor: re-vendored from $SOURCE; vendor.lock re-pinned"
exec "$DEST/verify-gates.sh"
