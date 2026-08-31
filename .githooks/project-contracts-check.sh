#!/bin/bash
# project-contracts-check.sh - verify a repo's declared contracts still refer to
# things that exist.
#
# A `## Project contracts` block in .claudecode.md states facts: this command is
# the gate, these paths are generated, that file is the lock. Prose cannot enforce
# anything, but those referents are falsifiable, and the way a declaration goes
# wrong is not that its prose becomes untrue in spirit - it is that a file gets
# renamed, a script gets dropped, a make target gets folded into another, and the
# document goes on describing a repo that no longer exists. A stale contract is
# worse than no contract, because it is read as current.
#
# So this checks referents, not outcomes. It does NOT run the suites: CI already
# does that, one of them takes seventeen minutes, and another needs a dev server.
# What it asserts is that every path, npm script and make target the declaration
# names is still there to be run.
#
# Deliberately NOT checked, because each would produce noise rather than signal:
#   - glob patterns (`src/**/*.ts`) - a glob matching nothing may be correct today
#   - prose nouns that merely look like paths
#   - whether a declared gate passes - that is CI's job and this must stay fast
#
# Usage: project-contracts-check.sh [repo-dir]     (default: cwd)
# Exit:  0 all referents resolve, or no declaration present
#        1 at least one referent is missing
#        2 usage / unreadable declaration

set -uo pipefail

REPO="${1:-$PWD}"
DOC="$REPO/.claudecode.md"
SELF="project-contracts-check"

[ -d "$REPO" ] || { echo "$SELF: not a directory: $REPO" >&2; exit 2; }

if [ ! -f "$DOC" ]; then
  echo "$SELF: no .claudecode.md in $REPO - nothing to check."
  exit 0
fi

block=$(awk '/^## Project contracts/{f=1} f' "$DOC")
if [ -z "$block" ]; then
  echo "$SELF: .claudecode.md has no '## Project contracts' block - nothing to check."
  exit 0
fi

fail=0
checked=0
tracked=$(cd "$REPO" && git ls-files 2>/dev/null)

# Paths the declaration itself says are NOT in a clone. A `local-only:` field is a
# statement that a machine-local exclude hides something here and that a fresh
# checkout will not have it - so failing on it contradicts the very sentence being
# read. This cost a red CI run: engram declares .claude/RESUME.md local-only, the
# check passed on the machine that had the file and failed in CI, which is the
# machine-dependent verdict this whole script exists to make impossible.
# The field runs until a blank line or the next `key:`, which is how every one of
# these declarations is laid out.
local_only=$(printf '%s\n' "$block" | awk '
  /^[[:space:]]*local-only:/ { f = 1; print; next }
  f && /^[[:space:]]*$/ { f = 0 }
  f && /^[[:space:]]*[A-Za-z][A-Za-z0-9_-]*:[[:space:]]/ { f = 0 }
  f { print }
' | tr -s '[:space:]' '\n' | sed -e 's/^[`("]*//' -e 's/[`),.;:"]*$//' -e 's#^\./##')

note() { printf '  %-7s %s\n' "$1" "$2"; }

# --- npm scripts -------------------------------------------------------------
# `npm run <name>` is only a claim when the repo has a package.json to hold it.
if [ -f "$REPO/package.json" ]; then
  while read -r script; do
    [ -n "$script" ] || continue
    checked=$((checked + 1))
    if node -e "const p=require('$REPO/package.json');process.exit(p.scripts&&p.scripts['$script']?0:1)" 2>/dev/null; then
      note "ok" "npm run $script"
    else
      note "MISSING" "npm run $script  - named by the declaration, absent from package.json"
      fail=1
    fi
  done < <(printf '%s\n' "$block" | grep -oE 'npm run [a-zA-Z][a-zA-Z0-9:_-]*' | awk '{print $3}' | sort -u)
fi

# --- make targets ------------------------------------------------------------
while read -r line; do
  [ -n "$line" ] || continue
  dir=$(printf '%s' "$line" | sed -E 's/^make -C ([^ ]+).*/\1/')
  tgt=$(printf '%s' "$line" | sed -E 's/^make -C [^ ]+ +([a-zA-Z][a-zA-Z0-9_-]*).*/\1/')
  [ "$tgt" = "$line" ] && continue
  checked=$((checked + 1))
  if [ -f "$REPO/$dir/Makefile" ] && grep -qE "^${tgt}:" "$REPO/$dir/Makefile"; then
    note "ok" "make -C $dir $tgt"
  else
    note "MISSING" "make -C $dir $tgt  - no such target"
    fail=1
  fi
done < <(printf '%s\n' "$block" | grep -oE 'make -C [^ ]+ +[a-zA-Z][a-zA-Z0-9_-]*' | sort -u)

# --- file paths --------------------------------------------------------------
# Scans the WHOLE block, not just backticked tokens: the declarations were written
# by different hands and most of them write paths bare. An earlier version read
# only backticks and reported "0 referents" in four of eight repos - a check that
# checks nothing passes for free, which is the failure mode this whole exercise
# exists to catch.
#
# Only tokens carrying a known file extension are FAILED on. A bare directory or a
# prose fragment containing a slash ("coder/reasoning/review") is ambiguous, and a
# false alarm here would teach someone to ignore the check, which is worse than
# missing one.
while read -r raw; do
  [ -n "$raw" ] || continue
  path=$(printf '%s' "$raw" | sed -e 's/^[`("[:space:]]*//' -e 's/[`),.;:"]*$//' -e 's#^\./##')
  case "$path" in
    ""|*'*'*|*'{'*|*'<'*|*'>'*|*'$'*|*'|'*|http*|~*|/*|.config/*) continue ;;
  esac
  # Must be a repo-relative path, not a bare basename. Declarations name files in
  # prose by basename ("ci.yml", "check-doc-links.mjs") and those live at paths
  # this cannot guess; asserting them produced 27 false alarms in one repo, which
  # is how a check gets ignored. A slash means the author stated a location.
  case "$path" in
    */*) ;;
    *) continue ;;
  esac
  case "$path" in
    *.md|*.ts|*.tsx|*.js|*.mjs|*.py|*.sh|*.json|*.toml|*.yaml|*.yml|*.lock|*.conf) ;;
    *) continue ;;
  esac
  checked=$((checked + 1))
  # Resolve by suffix against tracked files, not only from the repo root. The
  # declarations write paths relative to wherever the author was standing -
  # "src/index.ts" in a monorepo means packages/<pkg>/src/index.ts - and
  # demanding root-relative form produced 19 false alarms in one repo. A path
  # that matches no tracked file at any depth is genuinely gone, which is the
  # only thing worth failing on.
  # A gitignored path is a generated artifact the repo deliberately does not
  # track, so naming one is correct and its absence proves nothing: it exists
  # only after a build. Asked of git rather than guessed from the wording.
  if [ -n "$local_only" ] && printf '%s\n' "$local_only" | grep -qxF "$path"; then
    note "ok" "$path (declared local-only)"
    continue
  fi
  # `git check-ignore` also honours .git/info/exclude and the user's global ignore
  # file, and NEITHER is committed - so trusting a bare yes/no makes this script
  # answer differently on the maintainer's machine than in CI. Read the source of
  # the rule with -v and accept it only when it came from a tracked .gitignore.
  ignored_by=$( (cd "$REPO" && git check-ignore -v "$path" 2>/dev/null) | head -1 | cut -d: -f1 )
  case "$ignored_by" in
    ""|/*|.git/info/exclude|*/.git/info/exclude) ;;
    *) note "ok" "$path (generated, gitignored)"; continue ;;
  esac
  # Resolve, then confirm the resolved file is ON DISK. `git ls-files` reads the
  # INDEX, so an earlier version passed for a file deleted from the working tree -
  # it matched the index entry and never looked. A mutation test caught it:
  # removing a declared file left the check green, which is the exact inert-gate
  # failure this script exists to prevent.
  hit=""
  if [ -e "$REPO/$path" ]; then
    hit="$path"
  else
    while read -r cand; do
      [ -n "$cand" ] || continue
      if [ -e "$REPO/$cand" ]; then hit="$cand"; break; fi
    done < <(printf '%s\n' "$tracked" | grep -E "(^|/)$(printf '%s' "$path" | sed 's/[.[\*^$]/\\&/g')$" || true)
  fi
  if [ -n "$hit" ]; then
    note "ok" "$path"
  else
    note "MISSING" "$path  - named by the declaration, no such file on disk"
    fail=1
  fi
done < <(printf '%s\n' "$block" | tr -s '[:space:]' '\n' | grep -E '[A-Za-z0-9_./-]+\.[a-z]+' | sort -u)

echo
if [ "$fail" -eq 0 ]; then
  echo "$SELF: OK - $checked referent(s) in $(basename "$REPO") all resolve."
else
  echo "$SELF: FAILED - the declaration in $(basename "$REPO")/.claudecode.md names things that are gone." >&2
  echo "$SELF: fix the declaration or restore what it names; do not delete the check." >&2
fi
exit "$fail"
