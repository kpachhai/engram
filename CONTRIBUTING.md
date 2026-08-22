# Contributing to engram

engram is **already tool-agnostic**. It is consumed by Claude Code, OpenCode (local
models), and any MCP client over the same protocol; no client gets a special path. The
cross-tool contract IS the MCP surface, so "stay compatible across tools" mostly reduces
to one rule: **keep the protocol surface stable.** A new feature must behave the same
regardless of which AI client calls it.

## The MCP surface is the contract

- **Keep the 6-tool core stable** (pinned invariant 6 in `CLAUDE.md`). The MCP wire
  format is stable for v1.x: only non-breaking additions are permitted - new optional
  fields, new tools. A field rename, a removed field, or a changed default is breaking
  and warrants v2.0, not a v1.x PR.
- **Pydantic at the boundary, additive only.** New fields ship with safe defaults
  (`extra="ignore"` on outputs); existing fields are never removed (invariant 7,
  forward-compatible markdown).
- **No client-specific behavior.** Don't branch on the caller. A tool that returns one
  shape for Claude and another for a local model breaks the shared contract.

## Conventions live in CLAUDE.md

Don't re-derive them here. `CLAUDE.md` is authoritative for the pinned invariants
(markdown is source of truth, the portability gates, two-layer enforcement, the stable
wire format), Pydantic at boundaries, atomic writes, parameterized SQL, no `shell=True`,
strict mypy, and the hermetic CLI-smoke discipline. Read it before changing code.

## Setup and quality gates

```bash
uv sync --all-extras --dev && uv run pre-commit install
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
./.githooks/verify-gates.sh && ./.githooks/planning-vocab-ratchet.sh --check .
```

Everything above runs from a fresh clone, with nothing outside the repo beyond what
`uv sync` and `pre-commit install` fetch for themselves. The PII scan is the one gate
that behaves differently on the maintainer's machine: it adds name, email and username
patterns from an optional machine-local file that only they have, so on your clone and
in CI it runs on structural patterns alone. `verify-gates.sh` prints which of the two
modes it ran in - a scan reporting "clean" with half its rules unloaded looks exactly
like a full pass otherwise.

CI runs ruff, mypy, pytest (property tests included) and the repo gates across Python
3.11/3.12 x macOS/Ubuntu. The benchmarks in `bench/` are run manually, not in CI. Coverage gate is 80% (line); test quality over coverage percentage. Write
tests first; add a hermetic CLI smoke test for every new subcommand. Update
`CHANGELOG.md` under `[Unreleased]`, and write an ADR in `docs/adr/` for any
ADR-worthy decision. Tests use synthetic data only - never real user thoughts (the
three-repo data-ownership model: this repo is code only, ever).

## House style and commits

Hyphens or semicolons, never em-dashes. Numbers over adjectives. Secrets stay out of
the repo - keep them wherever your machine keeps them, and read the PII discipline in
`CLAUDE.md` before adding paths, names or keys to committed content. Commits are
`git commit -S -s` (GPG sign + DCO); no `Co-Authored-By` / AI attribution. By
contributing you agree to the Apache-2.0 `LICENSE`.
