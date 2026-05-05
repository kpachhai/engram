# Changelog

All notable changes to engram will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The MCP tool surface is committed-stable for the v1.x lifetime per the API stability commitment in `02-TECHNICAL_DESIGN.md`.

## [Unreleased]

### Added

- Initial project scaffold per `10-CODE_QUALITY.md`: `pyproject.toml` (PEP 621),
  `ruff` lint + format config, `mypy` strict mode, `pytest` config with coverage,
  `pre-commit` hooks, GitHub Actions CI matrix (Python 3.11 + 3.12 across macOS + Ubuntu),
  Apache-2.0 license, `README.md`, `CONTRIBUTING.md`, this changelog.
- Phase 1 implementation plan (`docs/PHASE_1_PLAN.md`) authored via
  `superpowers:deep-plan` with critique pass; 21 ordered steps across 8 layers.
- Layer 0 foundations:
  - `engram.logging` - structlog config writing only to stderr; secret-shaped
    keys (api_key, token, password, x-brain-key, etc.) redacted before any
    renderer runs; text or JSON output.
  - `engram.errors` - `EngramError` base + 7 typed subclasses, each with a
    stable `error_code` for MCP error mapping.
  - `engram.utils.atomic_write` - durable atomic file writes for the markdown
    SoT layer; tempfile in same directory as destination, `F_FULLFSYNC` on
    macOS, parent-directory fsync after rename, mode 0600.
  - `engram.utils.fingerprint` - canonical body fingerprint per
    `02-TECHNICAL_DESIGN.md`: SHA-256 over normalized body (line-ending
    normalization, trailing-whitespace strip per line, trailing-blank-line
    strip, UTF-8 encode).
  - `engram.utils.file_naming` - `{prefix-dir}/{YYYYMMDDHHMMSS}-{slug}-{shortuuid12}.md`
    derivation; slug fallback to `thought`; UUID-v7 last-12-hex tail; path
    traversal + RTL-override character rejection.
  - `engram.utils.run_command` - safe subprocess wrapper enforcing
    `shell=False`; `run_git` helper pre-stages the four non-interactive env
    vars (`GIT_TERMINAL_PROMPT=0`, `GIT_MERGE_AUTOEDIT=no`, `GIT_ASKPASS=true`,
    `GIT_LFS_SKIP_SMUDGE=1`) per `02-TECHNICAL_DESIGN.md` Flow C.
- 109 tests covering all of the above plus property-based (hypothesis) tests
  for fingerprint stability, atomic-write byte/text round-trip, and filename
  uniqueness across 2000-capture batches; coverage at 96.17%.

[Unreleased]: https://github.com/kpachhai/engram/compare/...HEAD
