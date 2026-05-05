# Contributing to engram

Thank you for considering contributing.

## Scope of contributions

This repo holds **code, tests, and docs for the engram tool**. It does NOT hold any user thoughts. The three-repo data ownership model is non-negotiable:

1. The engram project repo (this one) - code only, ever.
2. Each user's vault - their thoughts, in their own private repo.
3. Other users' vaults - separately owned; cross-user sharing is peer-to-peer only.

Pull requests that introduce real user data (test fixtures, examples, screenshots of personal vaults) will be rejected. Test fixtures use synthetic data only.

## Development setup

Requirements: Python 3.11+, `uv`, `git`.

```bash
git clone https://github.com/kpachhai/engram
cd engram
uv sync --all-extras --dev
uv run pre-commit install
```

## Quality gates

Every change must pass these gates locally before opening a PR:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI runs the same gates plus the property-based tests and benchmarks across the Python 3.11 / 3.12 x macOS / Ubuntu matrix.

Coverage threshold is 80% (line). Care more about test quality than coverage percentage; tests that exist only to bump coverage are technical debt.

## Code quality bar

The bar is the lineage of FastAPI / Pydantic / Httpx, not "passing tests + valid Python." Read [`docs/superpowers/specs/2026-05-04-engram/10-CODE_QUALITY.md`](https://github.com/kpachhai/idea-forge/blob/main/docs/superpowers/specs/2026-05-04-engram/10-CODE_QUALITY.md) before submitting code.

Specifically:

- Pydantic v2 at boundaries (MCP tools, config, frontmatter, migration manifests)
- `dataclass(frozen=True, slots=True)` for internal data containers
- Composition over inheritance; max 2 levels deep
- All filesystem paths via `pathlib.Path`, never `str`
- All UUIDs via `uuid.UUID`
- All datetimes timezone-aware (UTC)
- All YAML loaded with `yaml.safe_load` (or ruamel safe variant)
- All file writes atomic (`.tmp` + fsync + rename)
- All subprocess calls in list form (never `shell=True`)
- All SQLite queries parameterized (never string-concatenated)
- No `print()`; use `structlog` to stderr
- No bare `except:`; catch specific exception types

## Pull request flow

1. Fork or branch from `main`
2. Open an issue describing the change before writing significant code (avoids wasted work)
3. Write tests first (TDD). Property-based tests (`hypothesis`) for storage / embedding invariants
4. Keep PRs small and focused (one logical change per PR)
5. Update `CHANGELOG.md` under `[Unreleased]`
6. Run the full quality-gate sequence locally
7. Open the PR with a clear description of motivation, change, and test plan

## ADRs

Non-trivial architectural decisions live as Architecture Decision Records in `docs/adr/`. If your change adds or revises an ADR-worthy decision, write the ADR before merging.

## Reporting issues

For security issues, please use GitHub Security Advisories (private). For functional bugs, please open a regular issue with a minimal reproduction.

## License

By contributing, you agree that your contributions will be licensed under the [Apache License, Version 2.0](LICENSE).
