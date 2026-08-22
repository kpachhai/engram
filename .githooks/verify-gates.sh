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

# The PII gate loads identity-specific patterns (name, emails, username) from a
# machine-local file that exists only on the maintainer's machine. Everywhere
# else - a fork, a CI runner - the scan silently runs on structural patterns
# alone. Silently is the problem: a gate reporting "clean" while half its rules
# never loaded reads exactly like a full pass.
if [[ -f "${PII_IDENTITY_FILE:-$HOME/.config/devkit/identity.json}" ]]; then
  note "pii-scan: identity patterns available (name, email and username checks active)"
else
  note "pii-scan: DEGRADED - no identity file, so structural patterns only; names, emails and usernames are NOT checked. Expected on a fork and in CI."
fi

# A gate that hangs is failing, and should be reported as HUNG rather than
# dying at the CI job limit where it reads as an infrastructure flake. GNU
# `timeout` is not on a stock macOS, though, and calling it anyway returned
# rc=127 from every gate - so the verifier reported the gates as broken on any
# machine without coreutils, which is the false alarm the timeout exists to
# prevent. Prefer timeout, then coreutils' gtimeout, else run without a hang
# guard and say so.
if command -v timeout >/dev/null 2>&1; then
  run_gate() { timeout 60 "$@"; }
elif command -v gtimeout >/dev/null 2>&1; then
  run_gate() { gtimeout 60 "$@"; }
else
  note "no timeout(1) on PATH - gates run without a hang guard (install coreutils to restore it)"
  run_gate() { "$@"; }
fi

probe="$(mktemp -d)"; trap 'rm -rf "$probe"' EXIT

# --- PII gate must flag a planted path and pass clean content ---
printf 'ordinary line\nsee /Users/someone/secret for details\n' >"$probe/planted.md"  # pii-allow: planted probe fixture
printf 'nothing sensitive here\n' >"$probe/clean.md"
out="$(run_gate bash "$ROOT/.githooks/pii-scan.sh" "$probe/planted.md" </dev/null 2>/dev/null)"; rc=$?
[[ "$rc" -eq 124 ]] && bad "pii-scan HUNG on planted input (60s timeout)"
hits="$(printf '%s' "$out" | grep -c .)"
if [[ "$rc" -ne 1 || "$hits" -lt 1 ]]; then
  bad "pii-scan did NOT flag planted PII (rc=$rc hits=$hits) - the gate is not working"
else note "pii-scan: planted PII flagged ($hits hit)"; fi
run_gate bash "$ROOT/.githooks/pii-scan.sh" "$probe/clean.md" </dev/null >/dev/null 2>&1; rc=$?
[[ "$rc" -eq 124 ]] && bad "pii-scan HUNG on clean input (60s timeout)"
[[ "$rc" -eq 0 ]] || bad "pii-scan flagged clean content (rc=$rc) - gate is over-firing"

# --- vocab gate must flag planted planning vocabulary ---
printf '# Phase 3: planted planning vocabulary\nx = 1\n' >"$probe/planted.py"
printf '# an ordinary comment\ny = 2\n' >"$probe/clean.py"
run_gate bash "$ROOT/.githooks/planning-vocab-scan.sh" "$probe/planted.py" </dev/null >/dev/null 2>&1; rc=$?
[[ "$rc" -eq 124 ]] && bad "planning-vocab-scan HUNG on planted input (60s timeout)"
[[ "$rc" -eq 1 ]] || bad "planning-vocab-scan did NOT flag planted vocabulary (rc=$rc)"
run_gate bash "$ROOT/.githooks/planning-vocab-scan.sh" "$probe/clean.py" </dev/null >/dev/null 2>&1; rc=$?
[[ "$rc" -eq 124 ]] && bad "planning-vocab-scan HUNG on clean input (60s timeout)"
[[ "$rc" -eq 0 ]] || bad "planning-vocab-scan flagged clean content (rc=$rc)"
note "planning-vocab: planted vocabulary flagged, clean content passes"

[[ "$FAIL" -eq 0 ]] || exit 1
note "OK"
