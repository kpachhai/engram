#!/bin/bash
# planning-vocab-ratchet.sh - turn planning-vocab-scan into a real CI gate.
#
# The scanner reports ~233 findings on this repo, all pre-existing. As a
# blocking gate that is permanently red, so CI ran it `continue-on-error` and
# nobody read it - an advisory nobody reads is not a gate. A ratchet fixes the
# asymmetry: pre-existing findings are recorded once in a baseline, and only
# findings absent from that baseline fail the build. Existing debt is frozen,
# new debt is blocked, and paying debt down never breaks anything.
#
# KEYING. A finding is keyed by (repo-relative path, sha1 of the matched line),
# with a count so repeated identical lines in one file are still counted. It is
# deliberately NOT keyed by line number: adding one line near the top of a file
# would renumber every finding below it, so a line-keyed baseline reports a
# wave of phantom "new" findings on an unrelated edit, and - worse - a genuinely
# new violation can land on a line number already in the baseline and pass
# silently. The cost of content-keying is that reformatting an accepted line
# re-keys it and it needs re-accepting; that is the right default, since an
# edited line deserves another look.
#
# PATTERNS PIN. The baseline stores a hash of planning-vocab-patterns.conf. A
# regex change can alter what matches on lines nobody touched, which would read
# as hundreds of new violations; on drift this refuses to compare and asks for a
# regenerate instead of failing the build with noise.
#
# The baseline lives in the scanned repo (.planning-vocab-baseline), not in
# $HOME: findings are a property of the repo, and one shared baseline in $HOME
# would leak one repo's accepted debt into every other repo's gate.
#
# Usage:
#   planning-vocab-ratchet.sh --check <repo>   exit 1 on findings not in baseline
#   planning-vocab-ratchet.sh --write <repo>   (re)generate the baseline
#
# Exit: 0 clean, 1 new findings (or baseline drift needing regeneration), 2 usage.

set -u

SELF="planning-vocab-ratchet"
BASELINE_NAME=".planning-vocab-baseline"

die() { echo "$SELF: $*" >&2; exit 2; }

MODE="${1:-}"
REPO="${2:-}"
case "$MODE" in
  --check|--write) ;;
  *) die "usage: $SELF {--check|--write} <repo>" ;;
esac
[ -n "$REPO" ] && [ -d "$REPO" ] || die "not a directory: ${REPO:-<missing>}"

REPO_ABS="$(cd "$REPO" && pwd -P)" || die "cannot resolve: $REPO"
BASELINE="$REPO_ABS/$BASELINE_NAME"

# Locate the scanner and its patterns file: script-adjacent first (repo-bundled
# checkout), then the dotfiles-installed copy. Same resolution order the scanner
# itself uses for its conf.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
SCAN=""
for cand in "$SCRIPT_DIR/planning-vocab-scan.sh" \
            "$SCRIPT_DIR/executable_planning-vocab-scan.sh" \
            "$HOME/.claude/scripts/planning-vocab-scan.sh"; do
  [ -f "$cand" ] && SCAN="$cand" && break
done
[ -n "$SCAN" ] || die "cannot locate planning-vocab-scan.sh"

CONF=""
for cand in "$SCRIPT_DIR/planning-vocab-patterns.conf" \
            "$HOME/.claude/scripts/planning-vocab-patterns.conf"; do
  [ -f "$cand" ] && CONF="$cand" && break
done
[ -n "$CONF" ] || die "cannot locate planning-vocab-patterns.conf"

# GNU form first: on Linux both exist, on macOS only shasum does. Ordering it
# this way matches the stat-fallback lesson already learned in this repo.
#
# Truncated to 16 hex chars on purpose. A full 40-char SHA1 is exactly the shape
# pii-scan.sh matches to catch commit SHAs and leaked keys, so a baseline full of
# them fails the PII gate - one gate's output breaking another. 16 chars is far
# more than enough to keep a few hundred findings distinct.
sha1of() {
  if command -v sha1sum >/dev/null 2>&1; then sha1sum | awk '{print substr($1,1,16)}'
  else shasum -a 1 | awk '{print substr($1,1,16)}'; fi
}

PATTERNS_SHA="$(sha1of < "$CONF")"

TMP="$(mktemp -d /tmp/pvr.XXXXXX)" || die "mktemp failed"
trap 'rm -rf "$TMP"' EXIT

# Run the scanner. It exits 1 when it finds anything, which is its normal
# reporting state and not an error here.
bash "$SCAN" --repo "$REPO_ABS" >"$TMP/raw" 2>/dev/null || true

# Normalise to "<sha1-of-content>\t<count>\t<relpath>".
#
# Scanner lines are "<file>:<lineno>:<content>". Paths are made repo-relative so
# a baseline written with `--repo .` still matches one checked with an absolute
# path - the scanner prefixes whatever form it was given, and CI and a local run
# do not use the same form.
: > "$TMP/pairs"
while IFS= read -r line; do
  [ -n "$line" ] || continue
  file="${line%%:*}"
  rest="${line#*:}"
  content="${rest#*:}"                 # drop the line number
  rel="${file#"$REPO_ABS"/}"
  rel="${rel#./}"
  printf '%s\t%s\n' "$(printf '%s' "$content" | sha1of)" "$rel" >> "$TMP/pairs"
done < "$TMP/raw"

sort "$TMP/pairs" | uniq -c \
  | awk '{ c=$1; h=$2; $1=""; $2=""; sub(/^[ \t]+/,""); printf "%s\t%s\t%s\n", h, c, $0 }' \
  | sort > "$TMP/current"

current_count=$(wc -l < "$TMP/current" | tr -d ' ')

if [ "$MODE" = "--write" ]; then
  {
    echo "# planning-vocab baseline: findings accepted as pre-existing debt."
    echo "# Only findings ABSENT from this file fail CI, so paying debt down is safe"
    echo "# and adding new planning vocabulary is not."
    echo "# Regenerate after a deliberate change:"
    echo "#   planning-vocab-ratchet.sh --write ."
    echo "# Keyed by content hash + path, never line number - see the script header."
    echo "# patterns-sha1: $PATTERNS_SHA"
    cat "$TMP/current"
  } > "$BASELINE"
  echo "$SELF: wrote $BASELINE ($current_count entries)"
  exit 0
fi

# --- check ---
[ -f "$BASELINE" ] || die "no baseline at $BASELINE - create one with: $SELF --write $REPO"

base_sha="$(grep -m1 '^# patterns-sha1:' "$BASELINE" | awk '{print $3}')"
if [ "$base_sha" != "$PATTERNS_SHA" ]; then
  echo "$SELF: planning-vocab-patterns.conf changed since the baseline was written." >&2
  echo "  baseline: ${base_sha:-<none>}" >&2
  echo "  current:  $PATTERNS_SHA" >&2
  echo "A pattern change alters what matches on untouched lines, so the old baseline" >&2
  echo "cannot be compared against. Review the diff, then: $SELF --write $REPO" >&2
  exit 1
fi

grep -v '^#' "$BASELINE" | grep -v '^[[:space:]]*$' | sort > "$TMP/baseline"

# New = a key absent from the baseline, or the same key appearing more times
# than the baseline accepted (a second copy of an already-accepted line is
# still new debt).
awk -F'\t' '
  NR==FNR { base[$1 FS $3] = $2; next }
  {
    key = $1 FS $3
    accepted = (key in base) ? base[key] : 0
    if ($2 + 0 > accepted + 0) printf "%s\t%d\n", $3, $2 - accepted
  }
' "$TMP/baseline" "$TMP/current" | sort > "$TMP/new"

if [ -s "$TMP/new" ]; then
  echo "" >&2
  echo "$SELF: NEW planning/process vocabulary (not in $BASELINE_NAME):" >&2
  # Sum per file: several distinct new findings in one file should read as one
  # line saying how many, not as the same path repeated N times.
  awk -F'\t' '{n[$1] += $2} END {for (f in n) printf "  %s (%d new)\n", f, n[f]}' \
    "$TMP/new" | sort >&2
  echo "" >&2
  echo "Pre-existing findings are frozen; these are additions." >&2
  echo "Fix - rewrite WHY without WHEN, or add a 'vocab-allow' comment on the line." >&2
  echo "If the addition is deliberate and correct: $SELF --write $REPO" >&2
  exit 1
fi

echo "$SELF: no new findings ($current_count total, all baselined)"
exit 0
