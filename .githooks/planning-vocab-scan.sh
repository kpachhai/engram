#!/usr/bin/env bash
# planning-vocab-scan.sh - Scan files for planning/process vocabulary.
#
# Sister tool to pii-scan.sh - flags "iteration N", "Phase N", "F<N>",
# "T-task" planning artifacts that pollute shipped code. Reusable across
# any repo where the "No Phase/Layer References in Code" rule applies.
#
# Patterns come from:
#   ~/.claude/scripts/planning-vocab-patterns.conf
#   (or planning-vocab-patterns.conf next to this script, for repo-bundled installs)
#
# Usage:
#   planning-vocab-scan.sh file1 file2 ...        # scan named files
#   planning-vocab-scan.sh --repo <path>          # scan tracked + untracked files in repo
#   planning-vocab-scan.sh --staged               # scan git-staged files only
#   git diff --cached --name-only | planning-vocab-scan.sh   # files from stdin
#
# Per-line bypass: include `vocab-allow` substring on the same line.
#
# Path allowlist (always skipped):
#   - CHANGELOG.md / CHANGELOG-*.md  (release headers legitimately reference versions)
#   - .git/*                         (commit messages legitimately reference phases)
#   - docs/superpowers/*             (planning artifacts, typically gitignored)
#   - workspace/*-meta/*             (idea-forge retrospectives, intentional)
#   - node_modules/*, .venv/*, dist/*, build/*  (vendored / built)
#
# Exit codes:
#   0 = clean, no matches
#   1 = matches found (printed to stdout: file:line:content)
#   2 = error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
if [[ -f "${SCRIPT_DIR}/planning-vocab-patterns.conf" ]]; then
  PATTERNS_FILE="${SCRIPT_DIR}/planning-vocab-patterns.conf"
else
  PATTERNS_FILE="${HOME}/.claude/scripts/planning-vocab-patterns.conf"
fi

die() { echo "planning-vocab-scan: $*" >&2; exit 2; }

[[ -f "$PATTERNS_FILE" ]] || die "patterns file not found: $PATTERNS_FILE"

# --- Build patterns ---
patterns=()
while IFS= read -r line; do
  trimmed="${line#"${line%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  [[ -z "$trimmed" || "$trimmed" =~ ^# ]] && continue
  patterns+=("$trimmed")
done < "$PATTERNS_FILE"

[[ ${#patterns[@]} -gt 0 ]] || { echo "planning-vocab-scan: no patterns loaded - scan is a NO-OP; check ${PATTERNS_FILE} has uncommented pattern lines" >&2; exit 0; }

# --- Path allowlist (file path substring matches - always skip) ---
ALLOW_PATH_PATTERNS=(
  "CHANGELOG.md"
  "CHANGELOG-"
  "/.git/"
  "/docs/superpowers/"
  "-meta/"
  "/node_modules/"
  "/.venv/"
  "/dist/"
  "/build/"
  "planning-vocab-patterns.conf"
  "planning-vocab-scan.sh"
  # The ratchet's own baseline records the PATHS of accepted findings, and some
  # of those paths contain the very vocabulary being matched (a skill named
  # deep-plan, for one). Left unskipped, recording a finding creates a finding.
  ".planning-vocab-baseline"
  "planning-vocab-ratchet.sh"
)

path_allowed() {
  local f="$1"
  for p in "${ALLOW_PATH_PATTERNS[@]}"; do
    case "$f" in *"$p"*) return 0 ;; esac
  done
  return 1
}

# --- Collect files ---
files=()

if [[ "${1:-}" == "--staged" ]]; then
  while IFS= read -r f; do
    [[ -n "$f" && -f "$f" ]] && files+=("$f")
  done < <(git diff --cached --name-only --diff-filter=AM 2>/dev/null || true)
elif [[ "${1:-}" == "--repo" ]]; then
  REPO_PATH="${2:?--repo requires a path}"
  [[ -d "$REPO_PATH" ]] || die "not a directory: $REPO_PATH"
  pushd "$REPO_PATH" >/dev/null
  # Tracked files only - keep scope sensible
  while IFS= read -r f; do
    [[ -n "$f" && -f "$f" ]] && files+=("${REPO_PATH%/}/$f")
  done < <(git ls-files 2>/dev/null || true)
  popd >/dev/null
else
  for arg in "$@"; do
    [[ "$arg" == "-" ]] && continue
    files+=("$arg")
  done
  # Only drain stdin when the caller named no files. `[[ ! -t 0 ]]` alone reads
  # "stdin is not a terminal" as "a file list is being piped in", so passing
  # explicit paths from any script, CI step or non-interactive hook blocked
  # forever on a read that never returns. Same defect, same fix as pii-scan.sh;
  # these two were the only scripts here carrying it.
  #
  # A hung gate is worse than a failing one: it burns the job timeout and
  # surfaces as an infrastructure flake rather than a gate failure, and nobody
  # debugs a flake. Callers should still pass `</dev/null` and wrap in
  # `timeout`, but a gate must not depend on its caller getting that right.
  if [[ ${#files[@]} -eq 0 ]] && [[ ! -t 0 ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] && files+=("$f")
    done
  fi
fi

[[ ${#files[@]} -gt 0 ]] || exit 0

# --- Scan ---
combined_pattern="$(IFS='|'; echo "${patterns[*]}")"

found=0
# Bash 3.2 has no assoc arrays - emit lines, count via post-processing
TMPOUT="$(mktemp -t planning-vocab.XXXXXX)"
trap 'rm -f "$TMPOUT"' EXIT

for f in "${files[@]}"; do
  [[ -f "$f" ]] || continue
  path_allowed "$f" && continue
  if file --brief --mime-encoding "$f" 2>/dev/null | grep -qE '^(binary|application/)'; then
    continue
  fi
  matches="$(grep -nE "$combined_pattern" "$f" 2>/dev/null || true)"
  [[ -z "$matches" ]] && continue
  while IFS= read -r m; do
    [[ -z "$m" ]] && continue
    if printf '%s' "$m" | grep -q 'vocab-allow'; then
      continue
    fi
    # Skip line if T<N> match is actually a timestamp (T00:00, T23:59 etc)
    if printf '%s' "$m" | grep -qE 'T[0-9][0-9]:[0-9][0-9]'; then
      # Re-check whether the line has any OTHER planning vocab beyond timestamps
      stripped="$(printf '%s' "$m" | sed -E 's/T[0-9][0-9]:[0-9][0-9](:[0-9][0-9])?Z?//g')"
      if ! printf '%s' "$stripped" | grep -qE "$combined_pattern"; then
        continue
      fi
    fi
    # Skip line if F1 match is actually the ML metric (F1 score, table column,
    # precision/recall/F1, etc.)
    if printf '%s' "$m" | grep -qiE '(precision[/, |]+recall[/, |]+F1|F1[ -]score|/F1\b|recall[/, |]+F1|recall \| F1)'; then
      # Use perl for proper word-boundary support (BSD sed lacks \b)
      stripped="$(printf '%s' "$m" | perl -pe 's/\bF1\b//g' 2>/dev/null || printf '%s' "$m")"
      if ! printf '%s' "$stripped" | grep -qE "$combined_pattern"; then
        continue
      fi
    fi
    printf '%s:%s\n' "$f" "$m" | tee -a "$TMPOUT"
    found=1
  done <<< "$matches"
done

if [[ $found -eq 1 ]]; then
  echo "" >&2
  echo "=== Summary by file (count, path) ===" >&2
  awk -F: '{print $1}' "$TMPOUT" | sort | uniq -c | sort -rn >&2
  cat >&2 <<EOF

planning-vocab-scan: planning/process vocabulary detected.
Why it matters: phase/iteration labels are opaque to fresh readers and leak
internal planning history into shipped artifacts.
Fix - rewrite WHY without WHEN: replace "F6 (iteration 6): foo" with the
underlying reason, e.g. "release-attribution: foo".
Per-line bypass: add "vocab-allow" comment on the same line.
EOF
  exit 1
fi

exit 0
