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
#   planning-vocab-scan.sh --staged               # scan the INDEX blobs of staged files
#   planning-vocab-scan.sh --tsv [...]            # machine-readable "<path>\t<line>\t<content>"
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
#   1 = matches found (stdout: file:line:content, or TSV rows under --tsv)
#   2 = error, including "the file list resolved to nothing" - a scan that
#       examined no files is not a clean scan. `--staged` against an empty index
#       is the single exception and still exits 0: a deletion-only commit stages
#       no content, so there is genuinely nothing to read.

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

# Exit 2, not 0. A scanner that has loaded zero rules and reports success is
# failing OPEN: every caller downstream reads exit 0 as "checked, clean" when
# nothing was checked at all. The stderr warning alone is not enough - it is
# read by humans, while the exit code is what gates act on. This matches
# pii-scan.sh, whose caller in meta-stack-eval.sh now distinguishes "did not
# run" from "ran and found nothing".
[[ ${#patterns[@]} -gt 0 ]] || { echo "planning-vocab-scan: no patterns loaded - scan is a NO-OP; check ${PATTERNS_FILE} has uncommented pattern lines" >&2; exit 2; }

# --- Path allowlist (file path substring matches - always skip) ---
ALLOW_PATH_PATTERNS=(
  "CHANGELOG.md"
  "CHANGELOG-"
  # A skill's archived version entries are a changelog under another name: they
  # legitimately name the phases and sections each release changed.
  "version-history.md"
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

# --tsv is a mode flag, not a file selector, so strip it before the
# --staged/--repo/file-list chain below rather than adding a fourth branch.
TSV=0
_argv=()
for _a in "$@"; do
  if [[ "$_a" == "--tsv" ]]; then TSV=1; else _argv+=("$_a"); fi
done
set -- ${_argv[@]+"${_argv[@]}"}

# --- Collect files ---
files=()

STAGED=0
if [[ "${1:-}" == "--staged" ]]; then
  STAGED=1
  # Enumerate as its own step and read ITS status, exactly as --repo does below.
  # The old form ended `2>/dev/null || true`, which threw away git's exit code
  # and its diagnosis together: outside a work tree (git exits 129), against an
  # unreadable index, or under a safe.directory refusal, the loop ran zero
  # times, and zero staged paths then hit the empty-corpus carve-out further
  # down and exited 0. That carve-out is only true when the enumeration
  # SUCCEEDED and returned nothing, and nothing was reading whether it had.
  staged_list="$(git diff --cached --name-only --diff-filter=AM)" && staged_rc=0 || staged_rc=$?
  [[ $staged_rc -eq 0 ]] || die "git diff --cached failed (exit $staged_rc) - a staging area that cannot be enumerated has not been scanned clean"
  while IFS= read -r f; do
    # No -f test here. In --staged mode the subject is the index blob, and a
    # path can be staged and then cleaned or deleted in the worktree; requiring
    # it on disk silently dropped exactly the case the gate exists to catch.
    [[ -n "$f" ]] && files+=("$f")
  done <<< "$staged_list"
elif [[ "${1:-}" == "--repo" ]]; then
  REPO_PATH="${2:?--repo requires a path}"
  [[ -d "$REPO_PATH" ]] || die "not a directory: $REPO_PATH"
  pushd "$REPO_PATH" >/dev/null
  # Tracked files only - keep scope sensible.
  #
  # Run the enumeration as its own step and read ITS status. The old form was
  # `done < <(git ls-files 2>/dev/null || true)`, which discarded both the error
  # and the exit code: outside a work tree - a git-archive export, a build
  # context that drops .git, a safe.directory refusal - git printed nothing,
  # the loop ran zero times, and zero files became zero findings became a pass.
  # This repo's baseline holds hundreds of entries, so that collapse from a full
  # count to zero is the entire signal, and nothing was reading it.
  # `x="$(cmd)"` on its own would abort here under `set -e` with git's raw exit
  # code and none of the diagnosis below, so the status is captured through an
  # explicit `||` instead - the same reason a gate never reads a status through
  # a pipe.
  listing="$(git ls-files)" && ls_rc=0 || ls_rc=$?
  popd >/dev/null
  [[ $ls_rc -eq 0 ]] || die "git ls-files failed in $REPO_PATH (exit $ls_rc) - a repo that cannot be enumerated has not been scanned clean"
  while IFS= read -r f; do
    [[ -n "$f" && -f "${REPO_PATH%/}/$f" ]] && files+=("${REPO_PATH%/}/$f")
  done <<< "$listing"
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

# A file list that resolved to nothing is not a clean scan. Scanning this repo's
# several hundred tracked files and scanning none were byte-identical here -
# exit 0, no output - so a mistyped pathspec, a `--repo` pointed at a fresh
# checkout, or a caller whose stdin list came back empty all read as "checked,
# clean" about a corpus nobody enumerated. Refuse with the same exit 2 die()
# uses, which the ratchet's `scan_rc <= 1` guard already turns into a hard stop
# instead of zero new findings.
#
# `--staged` is the one mode where empty is genuinely real, and is exempt on
# purpose: a deletion-only commit stages no content, so there is nothing for
# this gate to read and that is a true no-op rather than a discovery failure.
# The exemption is only sound because the enumeration status above is now read -
# an empty list there means git succeeded and returned nothing, not that git
# failed.
if [[ ${#files[@]} -eq 0 ]]; then
  if [[ "$STAGED" -eq 1 ]]; then
    exit 0
  fi
  echo "planning-vocab-scan: 0 files to scan - a file list that resolved to nothing is not a clean result." >&2
  echo "  Pass --staged, --repo <path>, explicit paths, or pipe a file list; check the caller's pathspec." >&2
  exit 2
fi

# --- Scan ---
combined_pattern="$(IFS='|'; echo "${patterns[*]}")"

# In --staged mode the thing a commit publishes is the INDEX blob, not the file
# on disk. Reading the worktree let `git add <file-with-vocab>` followed by
# cleaning that file pass the gate while the vocabulary still went into the
# commit. Outside --staged mode the named file is what was asked for.
content_of() {
  if [[ "$STAGED" -eq 1 ]]; then
    git show ":$1" 2>/dev/null
  else
    cat -- "$1" 2>/dev/null
  fi
}

found=0
# Bash 3.2 has no assoc arrays - emit lines, count via post-processing
TMPOUT="$(mktemp -t planning-vocab.XXXXXX)"
trap 'rm -f "$TMPOUT"' EXIT

for f in "${files[@]}"; do
  [[ "$STAGED" -eq 1 || -f "$f" ]] || continue
  path_allowed "$f" && continue
  if content_of "$f" | file --brief --mime-encoding - 2>/dev/null | grep -qE '^(binary|application/)'; then
    continue
  fi
  # grep exits 0=match, 1=no match, >=2=error. Collapsing all three with
  # `|| true` turns one malformed pattern into a silent clean scan.
  matches="$(content_of "$f" | grep -nE "$combined_pattern")" || {
    rc=$?
    if [[ $rc -ge 2 ]]; then
      die "grep failed on ${f} (exit ${rc}) - check ${PATTERNS_FILE} for a malformed pattern. Refusing to report a clean scan."
    fi
    continue
  }
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
    printf '%s:%s\n' "$f" "$m" >> "$TMPOUT"
    if [[ $TSV -eq 1 ]]; then
      # grep -n always emits "<lineno>:<content>", and a line number contains no
      # colon, so this split is exact. Putting the path in its own field is what
      # makes a path containing a colon safe - the human "path:line:content" form
      # is ambiguous for those and silently corrupts a consumer's split.
      printf '%s\t%s\t%s\n' "$f" "${m%%:*}" "${m#*:}"
    else
      printf '%s:%s\n' "$f" "$m"
    fi
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
