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
IDENTITY_FILE="${HOME}/.config/devkit/identity.json"

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
structural_pattern="$(IFS='|'; echo "${patterns[*]}")"

# Identity-specific patterns (each value is regex-escaped to match as literal string)
if [[ -f "$IDENTITY_FILE" ]] && command -v jq >/dev/null 2>&1; then
  for field in full_name email_personal email_work github_username; do
    value="$(jq -r ".${field} // empty" "$IDENTITY_FILE" 2>/dev/null || true)"
    [[ -z "$value" || "$value" == "null" ]] && continue
    # Escape ERE metacharacters: . [ ] ( ) { } | + * ? ^ $ \ /
    escaped="$(printf '%s' "$value" | sed -e 's/[][\\\/.*^$?+|(){}]/\\&/g')"
    patterns+=("$escaped")
  done
fi

[[ ${#patterns[@]} -gt 0 ]] || { echo "pii-scan: no patterns loaded" >&2; exit 0; }

# --- Collect files ---

files=()

if [[ "${1:-}" == "--staged" ]]; then
  # Only files staged for add/modify in git (skips deletions, renames-without-content-change)
  while IFS= read -r f; do
    [[ -n "$f" && -f "$f" ]] && files+=("$f")
  done < <(git diff --cached --name-only --diff-filter=AM 2>/dev/null || true)
else
  # Files from args
  for arg in "$@"; do
    files+=("$arg")
  done
  # Files from stdin (if piped in)
  if [[ ! -t 0 ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] && files+=("$f")
    done
  fi
fi

[[ ${#files[@]} -gt 0 ]] || exit 0

# --- Scan ---

combined_pattern="$(IFS='|'; echo "${patterns[*]}")"

found=0
for f in "${files[@]}"; do
  [[ -f "$f" ]] || continue
  # Auto-skip pattern definition files and the scanner itself (self-reference loop).
  case "$f" in
    *pii-patterns.conf|*pii-scan.sh) continue ;;
  esac
  # Skip binary
  if file --brief --mime-encoding "$f" 2>/dev/null | grep -qE '^(binary|application/)'; then
    continue
  fi
  matches="$(grep -nE "$combined_pattern" "$f" 2>/dev/null || true)"
  [[ -z "$matches" ]] && continue
  # Filter out lines containing `pii-allow` marker (legitimate meta-discussion).
  while IFS= read -r m; do
    if printf '%s' "$m" | grep -q 'pii-allow'; then
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
    # cannot carry an inline pii-allow marker, so exempt the author(s) key line,
    # but still flag structural PII (paths, employer brand, keys) on it.
    case "$f" in
      package.json|*/package.json|*pyproject.toml|*Cargo.toml)
        if printf '%s' "$m" | grep -qE '^[0-9]+:[[:space:]]*"?authors?"?[[:space:]]*[:=]'; then
          printf '%s' "$m" | grep -qE "$structural_pattern" || continue
        fi ;;
    esac
    printf '%s:%s\n' "$f" "$m"
    found=1
  done <<< "$matches"
done

if [[ $found -eq 1 ]]; then
  cat >&2 <<EOF

pii-scan: PII patterns matched in staged content.
Fix the content (preferred) or document a false-positive exception before
proceeding. To bypass once (rarely correct), use: git commit --no-verify
EOF
  exit 1
fi

exit 0
