#!/usr/bin/env bash
# pii-scan.sh - Scan files for PII patterns.
#
# Patterns come from two sources:
#   1. ~/.claude/scripts/pii-patterns.conf - generic structural patterns
#      (committed, public, machine-shared).
#   2. ~/.config/devkit/identity.json - dynamic identity-specific patterns
#      (gitignored, machine-local). Adds literal-string regexes for the
#      user's full_name, email_personal, email_work, github_username.
#
# When identity.json is missing, only structural patterns are used.
#
# Usage:
#   pii-scan.sh file1 file2 ...              # scan named files (whole content)
#   git diff --cached --name-only | pii-scan.sh   # scan files from stdin
#   pii-scan.sh --staged                     # scan files staged in git (added+modified only)
#
# Exit codes:
#   0 = clean, no matches
#   1 = matches found (printed to stdout: file:line:content)
#   2 = error (missing config, etc.)

set -euo pipefail

# Self-locating: prefer a pii-patterns.conf next to this script (the
# per-repo bundling pattern), otherwise fall back to the dotfiles-managed
# canonical location. This keeps the same script working when it's
# installed at ~/.claude/scripts/ via dotfiles AND when it's bundled
# inside a repo at <repo>/.githooks/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
if [[ -f "${SCRIPT_DIR}/pii-patterns.conf" ]]; then
  PATTERNS_FILE="${SCRIPT_DIR}/pii-patterns.conf"
else
  PATTERNS_FILE="${HOME}/.claude/scripts/pii-patterns.conf"
fi
# identity.json is always machine-local (gitignored, outside any repo).
# Repo-bundled installs and dotfiles-managed installs both read the same path.
#
# PII_IDENTITY_FILE overrides the path. This is the test seam (same role as
# MCP_HEALTH_LIST_FILE in mcp-health-check.sh): the identity branch below is the
# half of this gate CI could never exercise, because the real identity.json is
# machine-local and must never reach a runner. With the seam, a synthetic
# identity proves the branch fires without putting a real name or address
# anywhere near CI logs.
IDENTITY_FILE="${PII_IDENTITY_FILE:-${HOME}/.config/devkit/identity.json}"

die() { echo "pii-scan: $*" >&2; exit 2; }

[[ -f "$PATTERNS_FILE" ]] || die "patterns file not found: $PATTERNS_FILE (run chezmoi apply to materialize)"

# --- Build patterns array ---

patterns=()

# Structural patterns from conf (skip comments + blanks)
while IFS= read -r line; do
  trimmed="${line#"${line%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  [[ -z "$trimmed" || "$trimmed" =~ ^# ]] && continue
  patterns+=("$trimmed")
done < "$PATTERNS_FILE"

# Structural-only pattern set (conf patterns, before identity values are added).
# Used to keep manifest author lines honest: the maintainer's name/email is
# allowed there, but paths, employer brands, and keys are not.
# `${patterns[*]:-}` not `${patterns[*]}`: /usr/bin/env bash here is GNU bash
# 3.2.57, and under `set -u` bash 3.2 treats an empty array expansion as an
# unbound variable and dies at this line. That made the zero-pattern case
# platform-divergent - crash on macOS, exit 0 on Linux CI - so neither platform
# saw the real behaviour. Fixing this is what lets the guard below be reached.
structural_pattern="$(IFS='|'; echo "${patterns[*]:-}")"

# Identity-specific patterns (each value is regex-escaped to match as literal string)
# Without jq (or without identity.json) the scan silently drops every
# identity pattern - the real name, both emails, the GitHub username - and
# keeps only structural ones. That is a large, invisible capability loss in a
# gate whose whole job is catching exactly those strings, so say so.
if [[ -f "$IDENTITY_FILE" ]] && ! command -v jq >/dev/null 2>&1; then
  echo "pii-scan: jq not found - identity patterns (name, emails, username) are NOT loaded; structural patterns only." >&2
fi
if [[ -f "$IDENTITY_FILE" ]] && command -v jq >/dev/null 2>&1; then
  for field in full_name email_personal email_work github_username; do
    value="$(jq -r ".${field} // empty" "$IDENTITY_FILE" 2>/dev/null || true)"
    [[ -z "$value" || "$value" == "null" ]] && continue
    # Escape ERE metacharacters: . [ ] ( ) { } | + * ? ^ $ \ /
    escaped="$(printf '%s' "$value" | sed -e 's/[][\\\/.*^$?+|(){}]/\\&/g')"
    patterns+=("$escaped")
  done
fi

# Exit 2, not 0. A scanner that has loaded zero rules and reports success is
# failing OPEN: every caller downstream reads exit 0 as "checked, clean" when
# nothing was checked at all. That is the same class of defect as a false
# negative from a scan that never ran, and for a gate guarding PII it is the
# worst available outcome. Exit 2 matches die() and the no-files-given refusal.
[[ ${#patterns[@]} -gt 0 ]] || { echo "pii-scan: no patterns loaded - scan is a NO-OP; check ${PATTERNS_FILE} has uncommented pattern lines" >&2; exit 2; }

# --- Collect files ---

files=()

# No arguments and nothing piped in means the caller named no files at all.
# Collecting zero files and exiting 0 would read as "scanned, clean" - the
# most dangerous possible failure for a security gate - so refuse instead.
# `--staged` with an empty staging area is a genuine no-op and still exits 0.
if [[ $# -eq 0 ]] && [[ -t 0 ]]; then
  echo "pii-scan: no files given. Pass --staged, explicit paths, or pipe a file list." >&2
  exit 2
fi

# An unrecognised flag must not fall through to the treat-args-as-filenames
# path. `pii-scan.sh --stagd` used to exit 0 having scanned a file named
# "--stagd" that does not exist - a typo in a hook or CI line silently disabled
# the gate and looked like a pass. `-` is the explicit stdin marker; a real file
# whose name begins with a dash can still be passed as ./-name.
for arg in "$@"; do
  case "$arg" in
    --staged|-) ;;
    -*) die "unrecognised option: $arg (expected --staged, -, or file paths)" ;;
  esac
done

STAGED=0
if [[ "${1:-}" == "--staged" ]]; then
  STAGED=1
  # Every staged path whose content lands in the commit: added, copied,
  # modified, renamed, type-changed. Only deletions are excluded (no content).
  # `AM` alone silently skipped renames, so `git mv` moved PII past the gate.
  # -z + read -d '' keeps paths with spaces or non-ASCII bytes intact, which
  # the plain --name-only form mangles and the old -f test then dropped.
  while IFS= read -r -d '' f; do
    [[ -n "$f" ]] && files+=("$f")
  done < <(git diff --cached -z --name-only --diff-filter=ACMRT 2>/dev/null || true)
else
  # Files from args (`-` is the stdin marker, never a filename)
  for arg in "$@"; do
    [[ "$arg" == "-" ]] && continue
    files+=("$arg")
  done
  # Files from stdin, but ONLY when the caller named none. The old condition was
  # `[[ ! -t 0 ]]` alone, which reads "stdin is not a terminal" as "a file list
  # is being piped in" - so `pii-scan.sh somefile` from any script, CI step or
  # non-interactive hook blocked forever on a read that would never return.
  # Since this scanner runs from the git pre-commit path and never as a
  # registered Claude hook, non-interactive IS its normal caller.
  #
  # Gating on "no file arguments" rather than on an explicit --stdin flag is
  # deliberate: `git ls-files | pii-scan.sh` is the form .github/workflows/
  # harness-check.yml uses, and requiring a new flag would have made that line
  # scan nothing while still exiting 0. Fixing a hang by disabling the gate in
  # CI would have been a worse bug than the hang.
  if [[ ${#files[@]} -eq 0 ]] && [[ ! -t 0 ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] && files+=("$f")
    done
  fi
fi

[[ ${#files[@]} -gt 0 ]] || exit 0

# --- Scan ---

combined_pattern="$(IFS='|'; echo "${patterns[*]}")"

# In --staged mode the thing being published is the INDEX blob, not the file on
# disk. Reading the worktree let `git add <file-with-pii>` followed by cleaning
# or deleting that file pass the gate while the PII still went into the commit.
# It also means a symlink is scanned as its stored target path rather than by
# following the link. Outside --staged mode the named file is what was asked for.
content_of() {
  if [[ "$STAGED" -eq 1 ]]; then
    git show ":$1" 2>/dev/null
  else
    cat -- "$1" 2>/dev/null
  fi
}

found=0
for f in "${files[@]}"; do
  [[ "$STAGED" -eq 1 || -f "$f" ]] || continue
  # Auto-skip pattern definition files and the scanner itself (self-reference loop).
  case "$f" in
    *pii-patterns.conf|*pii-scan.sh) continue ;;
  esac
  # Auto-skip machine-generated dependency lockfiles: their content hashes
  # (sha256 etc.) false-positive against the hex key patterns, and humans
  # never author PII into them.
  case "$f" in
    *uv.lock|*package-lock.json|*yarn.lock|*pnpm-lock.yaml|*Cargo.lock|\
    *poetry.lock|*Gemfile.lock|*composer.lock|*flake.lock|*skills-lock.json) continue ;;
  esac
  # Same rationale, different shape: files whose entire content is a machine
  # -written hash or an exported blob. The generic 40-hex-char pattern cannot
  # tell a git SHA from a legacy API key, so a commit-pin file matched on every
  # scan and could never be committed - a bare SHA has nowhere to put an inline
  # `pii-allow` marker. That taught `--no-verify`, which is worse than the gap.
  # Trade-off: a /Users/<name>/ path inside an exported plist is no longer
  # caught; re-export hygiene has to cover that.
  case "$f" in
    *PINNED_COMMIT|*PINNED_VERSION|*.plist) continue ;;
  esac
  # Skip binary
  if content_of "$f" | file --brief --mime-encoding - 2>/dev/null | grep -qE '^(binary|application/)'; then
    continue
  fi
  # -i because identity values and brand names appear in mixed case; the conf
  # listing a brand twice, once capitalised and once lowercased, was hand-rolling
  # exactly this. A gate that misses PERSON@EXAMPLE.COM is not a gate.
  #
  # grep exits 0=match, 1=no match, >=2=error. The old `|| true` collapsed all
  # three, so one malformed pattern in pii-patterns.conf turned the entire scan
  # into a silent pass. A scanner that cannot run must fail closed.
  matches="$(content_of "$f" | grep -niE "$combined_pattern")" || {
    rc=$?
    if [[ $rc -ge 2 ]]; then
      die "grep failed on ${f} (exit ${rc}) - check ${PATTERNS_FILE} for a malformed pattern. Refusing to report a clean scan."
    fi
    continue   # rc == 1: genuinely no match
  }
  [[ -z "$matches" ]] && continue
  # Skip lines carrying the documented allow marker. Requiring the colon keeps
  # an unrelated word that merely contains "pii-allow" from disabling a line.
  while IFS= read -r m; do
    if printf '%s' "$m" | grep -q 'pii-allow:'; then
      continue
    fi
    # Filter out documentation-placeholder paths. Real usernames don't use
    # ALL_CAPS_WITH_UNDERSCORES or angle-bracket syntax, so these are
    # unambiguous placeholders in docs (launchd plists, cron lines, install
    # snippets). Match the path-portion of the line and skip if it contains
    # any of these tokens.
    if printf '%s' "$m" | grep -qE '/(Users|home)/(YOUR_USERNAME|<your-username>|<USERNAME>|<username>|<user>|USERNAME)/'; then
      continue
    fi
    # Allow package-manifest attribution fields - the one sanctioned place for
    # the maintainer's name (see CLAUDE.md PII Discipline). JSON/TOML manifests
    # cannot carry an inline pii-allow marker, so exempt the author(s) key line
    # (single-line form), name-entry lines inside a multi-line authors block,
    # and the manifest's own repository-URL lines (Repository/Issues/Homepage
    # etc. self-references). Still flag structural PII (paths, employer brand,
    # keys) on any of them.
    case "$f" in
      package.json|*/package.json|*pyproject.toml|*Cargo.toml|\
      .claude-plugin/plugin.json|*/.claude-plugin/plugin.json|\
      .claude-plugin/marketplace.json|*/.claude-plugin/marketplace.json)
        if printf '%s' "$m" | grep -qE '^[0-9]+:[[:space:]]*("?authors?"?[[:space:]]*[:=]|\{[[:space:]]*name[[:space:]]*=|"name"[[:space:]]*:)'; then
          printf '%s' "$m" | grep -qE "$structural_pattern" || continue
        fi
        if printf '%s' "$m" | grep -qE '^[0-9]+:[[:space:]]*"?(Repository|Issues|Changelog|Homepage|Documentation|Source|repository|homepage|bugs|url|urls)"?[[:space:]]*[:=].*https?://'; then
          printf '%s' "$m" | grep -qE "$structural_pattern" || continue
        fi ;;
    esac
    printf '%s:%s\n' "$f" "$m"
    found=1
  done <<< "$matches"
done

if [[ $found -eq 1 ]]; then
  cat >&2 <<EOF

pii-scan: PII patterns matched (file:line:content above).
Why it matters: this content is bound for a public/forkable repo; PII that
lands in a commit requires a full history scrub (scrub-pii-history.sh) to undo.
Fix: rewrite the line generically per the CLAUDE.md PII Discipline table
(<Your Name>, your-employer, <your-username>), or for a true false-positive
add a 'pii-allow:<reason>' marker comment on that line.
Bypass once (rarely correct): git commit --no-verify
EOF
  exit 1
fi

exit 0
