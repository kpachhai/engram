#!/usr/bin/env bash
# Verify the vendored gate scripts in .githooks/.
#
# Two independent checks, because either alone is insufficient:
#
#   INTEGRITY - every file listed in vendor.lock must match its recorded
#   sha256. Identity is the HASH; the path is metadata. Keying on path or
#   name would let a first-party file that happens to share a name be
#   silently exempted.
#
#   DETECTION - each gate is run against planted input it MUST flag. A hash
#   check cannot tell you a correctly-vendored scanner still works, and a
#   scan that reports "clean" is indistinguishable from a scan that did
#   nothing. Planted fixtures are generated at runtime, never committed, so
#   they cannot rot and the scanners have nothing to special-case.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd -P)"
LOCK="$ROOT/.githooks/vendor.lock"
FAIL=0
note() { printf 'verify-gates: %s\n' "$*"; }
bad()  { printf 'verify-gates: FAIL - %s\n' "$*" >&2; FAIL=1; }

# shasum (perl) on macOS, sha256sum (coreutils) on Linux runners.
sha256_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else sha256sum "$1" | cut -d' ' -f1; fi
}

[[ -f "$LOCK" ]] || { bad "missing $LOCK"; exit 2; }

entries=0
# `cat; echo` guards the final line: a bare `while read` DROPS it when the
# file has no trailing newline, which would silently skip a locked entry.
while read -r want path _marker; do
  [[ -z "${want:-}" || "${want:0:1}" == "#" ]] && continue
  entries=$((entries + 1))
  target="$ROOT/$path"
  if [[ ! -f "$target" ]]; then bad "locked file absent: $path"; continue; fi
  got="$(sha256_of "$target")"
  [[ "$got" == "$want" ]] || bad "hash drift: $path
      locked  $want
      on disk $got
      These files are vendored from the maintainer's dotfiles. Re-vendor and
      regenerate vendor.lock; never hand-edit a vendored copy."
done < <(cat "$LOCK"; echo)

# A lockfile pinning nothing must fail. A verifier that checks zero entries
# and reports success is the exact failure this file exists to prevent.
[[ "$entries" -gt 0 ]] || bad "vendor.lock pins zero entries - verification would be a no-op"
note "integrity: checked $entries locked entries"

probe="$(mktemp -d)"; trap 'rm -rf "$probe"' EXIT

# --- PII gate must flag a planted path and pass clean content ---
printf 'ordinary line\nsee /Users/someone/secret for details\n' >"$probe/planted.md"  # pii-allow: planted probe fixture
printf 'nothing sensitive here\n' >"$probe/clean.md"
out="$(timeout 60 bash "$ROOT/.githooks/pii-scan.sh" "$probe/planted.md" </dev/null 2>/dev/null)"; rc=$?
[[ "$rc" -eq 124 ]] && bad "pii-scan HUNG on planted input (60s timeout)"
hits="$(printf '%s' "$out" | grep -c .)"
if [[ "$rc" -ne 1 || "$hits" -lt 1 ]]; then
  bad "pii-scan did NOT flag planted PII (rc=$rc hits=$hits) - the gate is not working"
else note "pii-scan: planted PII flagged ($hits hit)"; fi
timeout 60 bash "$ROOT/.githooks/pii-scan.sh" "$probe/clean.md" </dev/null >/dev/null 2>&1; rc=$?
[[ "$rc" -eq 124 ]] && bad "pii-scan HUNG on clean input (60s timeout)"
[[ "$rc" -eq 0 ]] || bad "pii-scan flagged clean content (rc=$rc) - gate is over-firing"

# --- vocab gate must flag planted planning vocabulary ---
printf '# Phase 3: planted planning vocabulary\nx = 1\n' >"$probe/planted.py"
printf '# an ordinary comment\ny = 2\n' >"$probe/clean.py"
timeout 60 bash "$ROOT/.githooks/planning-vocab-scan.sh" "$probe/planted.py" </dev/null >/dev/null 2>&1; rc=$?
[[ "$rc" -eq 124 ]] && bad "planning-vocab-scan HUNG on planted input (60s timeout)"
[[ "$rc" -eq 1 ]] || bad "planning-vocab-scan did NOT flag planted vocabulary (rc=$rc)"
timeout 60 bash "$ROOT/.githooks/planning-vocab-scan.sh" "$probe/clean.py" </dev/null >/dev/null 2>&1; rc=$?
[[ "$rc" -eq 124 ]] && bad "planning-vocab-scan HUNG on clean input (60s timeout)"
[[ "$rc" -eq 0 ]] || bad "planning-vocab-scan flagged clean content (rc=$rc)"
note "planning-vocab: planted vocabulary flagged, clean content passes"

[[ "$FAIL" -eq 0 ]] || exit 1
note "OK"
