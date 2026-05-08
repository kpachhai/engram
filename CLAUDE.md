# CLAUDE.md

This file gives Claude Code (claude.ai/code) the context it needs to work productively on the engram repository.

## What engram is

`engram` is a Model Context Protocol (MCP) server that gives AI assistants a persistent, portable, sovereign memory layer. Thoughts are markdown files; vector search runs locally; sync is `git push`. The full positioning + use cases live in `README.md`, `docs/USE_CASES.md`, and `docs/COMPARISONS.md`.

The thesis in one sentence: **markdown files are the source of truth, SQLite + sqlite-vec is a regenerable index, git is the sync mechanism, MCP is the API.**

## Repository layout

```
engram/
├── CLAUDE.md                         # this file
├── README.md                         # public-facing entry point
├── pyproject.toml                    # PEP 621 + ruff + mypy + pytest config
├── uv.lock                           # locked dependencies
├── .pre-commit-config.yaml           # ruff + mypy on changed files
├── .github/workflows/ci.yml          # CI matrix (3.11+3.12 × ubuntu+macos)
├── docs/
│   ├── QUICKSTART.md                 # 5-minute install + first capture
│   ├── USE_CASES.md                  # 5 personas with examples
│   ├── COMPARISONS.md                # vs Mem0 / Letta / basic-memory / etc.
│   ├── ARCHITECTURE.md               # internals (component diagram, flows)
│   ├── OPENBRAIN_MIGRATION_GUIDE.md  # operator walkthrough for OB1 → engram
│   ├── MULTI_MACHINE_SETUP.md        # personal-machine git sync
│   ├── MULTI_VAULT_SETUP.md          # role taxonomy + per-user config
│   ├── FRIEND_SHARE_GUIDE.md         # bundle export/import flow
│   ├── TEAM_BRAIN_GUIDE.md           # shared team vault + GPG attribution
│   ├── LLM_FEATURES.md               # opt-in summarize / synthesize tools
│   ├── PUBLISHING.md                 # PyPI release procedure (maintainer-only)
│   ├── DEPLOYMENT_MODEL.md           # local-first thesis (why not cloud-hosted)
│   ├── adr/                          # 7 ADRs (one per major design choice)
│   └── archive/
│       └── phases/                   # historical: PHASE_<N>_PLAN + PHASE_<N>_CODE_COMPLETE
├── src/engram/
│   ├── cli/                          # all engram CLI commands (typer-based)
│   ├── config/                       # 5-layer config loader + Pydantic models
│   ├── models/                       # Frontmatter, Thought, MCP I/O models
│   ├── storage/                      # markdown SoT + SQLite + sqlite-vec
│   ├── embedding/                    # FastEmbed wrapper + hash manifest
│   ├── multivault/                   # registry + cross-vault aggregator
│   ├── sync/                         # git sync coordinator state machine
│   ├── llm/                          # provider abstraction + budget + citations
│   ├── mcp/                          # FastMCP server + tool handlers
│   ├── team/                         # team-vault primitives + pre-receive hook
│   ├── bundle/                       # export/import bundle format
│   ├── migration/                    # Open Brain migration pipeline
│   ├── diagnostics/                  # engram doctor + 31 check codes
│   └── utils/                        # atomic_write, fingerprint, file_naming, lock
├── tests/                            # 1166 tests (unit + integration + smoke)
└── bench/                            # NFR1 search-latency benchmarks
```

The spec lives outside this repo at `~/repos/github.com/kpachhai/idea-forge/docs/superpowers/specs/2026-05-04-engram/` (the maintainer's planning repo). Treat the spec as historical context; the shipped repo is authoritative for what engram actually does today.

## Phase history (historical context, not active work)

engram shipped across four phases. The phase artifacts in `docs/archive/phases/PHASE_<N>_*.md`, `docs/adr/`, and `CHANGELOG.md` are the historical record:

| Phase | Scope |
|---|---|
| 1 | Solo MVP + Open Brain migration |
| 2 | Multi-machine personal sync (git transport) |
| 3 | Multi-vault foundation + friend-share + optional LLM tools |
| 4 | Team Brain (multi-target write + GPG attribution + per-prefix routing + server-side hook) |

All four are **code-complete**. Future work is operational dogfood (PyPI publish, multi-day team-vault exercise) and incremental polish, not phased delivery.

**Don't reintroduce "Phase N" framing in source comments.** That historical context belongs in plan / ADR / retrospective docs. Source code reads as a polished v1.0 product.

## Pinned invariants (DO NOT VIOLATE)

These are the load-bearing properties that every change must preserve:

1. **Markdown is the source of truth.** SQLite + sqlite-vec is a *regenerable* cache. If markdown and SQLite disagree, markdown wins. `engram reindex --full` rebuilds the index from markdown.
2. **`portability=block` thoughts NEVER reach an LLM.** No flag, config, or provider locality overrides this. Defense-in-depth: the resolver, the portability gate, AND every LLM tool entry point all enforce it independently.
3. **`portability=sensitive` thoughts only reach LOCAL LLM providers.** Ollama / llama.cpp only. Any remote provider (Anthropic, OpenAI) refuses sensitive thoughts at the resolver.
4. **Two-layer enforcement at security boundaries.** Client-side is canonical for capture-time policies (block routing, member enrollment); server-side is canonical for push-time policies (allowlists, attribution integrity, `.indexes/` containment, force-push refusal). The two layers compose — a single bypass doesn't breach the boundary.
5. **Sender identity binds to GPG primary-key fingerprint** (40 hex), not a free-form string. Team-vault captures refuse if the local key isn't in `members.yaml`.
6. **MCP wire format is stable for v1.x.** Only non-breaking additions (new optional fields, new tools) are permitted. Breaking changes warrant v2.0.
7. **Forward-compatible markdown.** Files written by today's engram MUST be readable by every future version. New schema versions add fields with safe defaults; existing fields are never removed.

## PII Discipline

This repo is publicly forkable. Follow the global PII rules in `~/.claude/CLAUDE.md` ("PII Discipline (publishable repos)" + "Pre-Write Checklist for Publishable Repos") plus these engram-specific notes:

- **`pyproject.toml` `authors` field** is the only place a real maintainer name is permitted in committed content (project-attribution exception, parallel to `package.json` `"author"` in idea-forge).
- **No employer or company brand names** in source, tests, docs, ADRs, or commit messages. The pinned-invariants list, the spec, and the migration guide should all be readable by any forker without context about who built engram.
- **No hardcoded `/Users/<name>/` paths.** Examples in docs use `~/.local/share/engram/personal` or similar generic paths; tests use `tmp_path` fixtures.
- **No companion-repo paths bearing the maintainer's GitHub username.** Cross-references to other repos use generic terms (`your dotfiles`, `your meta-stack repo`, `your persistent-memory MCP`) — except for the spec back-reference in `## Repository layout`, which IS the maintainer's planning repo and is acknowledged as such.
- **No GPG fingerprints, API keys, MCP URLs with secrets, or test fixtures derived from real keys** in committed content. Test GPG keys are generated in `tmp_path` per-test; OB1 migration secrets live in `~/.config/devkit/references.json` (machine-local, gitignored).

Run the Pre-Write Checklist before writing or editing any file in this repo. If candidate PII slips in, flag it before staging - active guidance over silent rewrites.

## Coding conventions

Follow the global rules in `~/.claude/CLAUDE.md` (use hyphens or semicolons instead of em-dashes, no emojis unless asked, fail-fast error handling, descriptive names, surgical changes only). Engram-specific additions:

- **Pydantic at every boundary.** All MCP I/O, all config files, all bundle manifests use Pydantic models with `model_config = ConfigDict(extra="forbid")` for inputs and `extra="ignore"` for outputs. New fields are additive only.
- **Atomic writes.** All file mutations go through `engram.utils.atomic_write.atomic_write_text` or `atomic_write_bytes` (tempfile + fsync + rename). Never write to the destination path directly.
- **Parameterized SQL.** All queries in `engram.storage.sqlite_queries` use `?`-placeholders. Never f-string user input into SQL. ruff S enforces this.
- **No shell=True.** Subprocess calls in `engram.utils.run_command` and `engram.sync.gitops` use list-form `subprocess.run(["cmd", "arg"], shell=False)`. ruff S enforces this.
- **Type-strict mypy.** `pyproject.toml` has `[tool.mypy] strict = true`. Every new function has type hints; `Any` is a code smell.
- **Per-vault locking.** Long-running serve processes acquire a `VaultLock` (advisory file lock via `fcntl.flock`) per vault. Never modify a vault from multiple processes without flock.
- **Failure-tolerant embedding.** If the embedder raises during capture, the thought is captured with `embedding_status='pending'` and `engram doctor --repair` regenerates later. Capture itself never fails because of embedding issues.

## Testing

```bash
uv sync --all-extras --dev          # install + lock deps
uv run pytest -q                     # full suite (1166 tests; ~2.5 min)
uv run ruff format                   # auto-format
uv run ruff check --fix              # lint + auto-fix
uv run mypy                          # strict type-check (188 source files)
uv run pytest --cov=src --cov-fail-under=80   # coverage gate
```

Test taxonomy:

- `tests/<module>/test_*.py` — unit tests; one file per source module.
- `tests/integration/` — cross-subsystem flows (multi-vault, sync convergence).
- `tests/properties/` — Hypothesis property tests for invariants.
- `tests/test_phase4_cli_smoke.py` — **hermetic CLI smoke against the installed binary**. This catches wiring bugs the handler-level tests miss (Typer registration, argument plumbing, exit codes, --help output). Add a smoke test for every new CLI subcommand.
- `tests/team/test_phase4_exit_criteria.py` — 23-scenario integration suite covering the team-vault pinned invariants end-to-end via in-process composition.

**Tests must be hermetic.** No network calls (use `httpx.MockTransport` for HTTP-backed providers). No real GPG keyring (mock `gpg --list-secret-keys` output via `subprocess` substitute). No cross-test SQLite reuse (every test owns its own `tmp_path`).

## Common operations

```bash
# Start an MCP server for the configured vault
uv run engram serve

# Health check (run after any config change or migration)
uv run engram doctor

# Migrate from Open Brain (one-time)
# WARNING: the MCP-based `migrate-from-open-brain` CLI is currently broken against
# real OB1 (OB1's MCP tools return human-readable text, not structured records).
# Use the Postgres-direct path instead - see docs/OPENBRAIN_MIGRATION_GUIDE.md
# for the reference script + walkthrough.

# Rebuild the index from markdown
uv run engram reindex --full

# Export a portable bundle for a friend
uv run engram export --output ~/share/bundle.tar.gz --portability portable

# Import a friend's bundle
uv run engram import ~/share/bundle.tar.gz --vault friend-vault --allow-read-only

# Bootstrap a team vault (steward)
uv run engram team-vault setup ~/team-vaults/postmortems --remote git@github.com:org/team-postmortems.git

# Add a team member (steward)
uv run engram team-vault add-member <40-hex-fingerprint> --members-yaml .engram/members.yaml --policy-yaml .engram/team-policy.yaml

# Print FastEmbed model hashes (after a model upgrade)
uv run engram doctor --download-model --print-hashes
```

## When making changes

**Discipline that's been load-bearing on this project:**

1. **Layer ordering: integration callsites in CLI/server BEFORE integration tests, not after.** Phase 3 + Phase 4 both got bitten by deferring CLI wiring to the end. If you're adding a new component, wire it into the user-facing path (CLI / `build_multivault_server` / `engram doctor`) in the same change that adds the component, not later.
2. **Hermetic CLI smoke is mandatory at exit.** Per `python-package-builder` Phase Exit Step 5: every new CLI subcommand or modified subcommand gets a smoke test in `tests/test_phase4_cli_smoke.py` that spawns the actual binary via subprocess. The test suite catches handler bugs; the smoke catches wiring bugs.
3. **Defense-in-depth at security boundaries.** When adding a new constraint (allowlist, refusal, gate), put it in two places: the capture-time client-side gate AND the push-time server-side check (when applicable). Single-layer enforcement is brittle.
4. **Spec-vs-implementation audit before claiming "done."** Run a sub-agent to walk the relevant spec doc and cross-check against `src/engram/` before merging. Three independent gaps (FastEmbed integrity, LLM CLI, doc count drift) escaped initial Phase 4 closeout because we didn't audit thoroughly.
5. **Verify-before-done at every "shipped" claim.** Stderr discipline (check both stdout AND stderr). Bounds-checks on numerical outputs. List what was NOT verified explicitly. The `verify-before-done` global skill produces the checklist.

## Operational reality

- **Embedding model:** `BAAI/bge-small-en-v1.5` is pinned. The hash manifest in `src/engram/embedding/model_hashes.py` is populated; mismatched files raise `EmbeddingError`. Recompute via `engram doctor --download-model --print-hashes` after any model upgrade.
- **Index location:** `<vault>/.indexes/engram.db` (gitignored).
- **Locks + state:** `<vault>/.engram/` holds per-machine state (identity, push queue, orphan tarballs); always gitignored.
- **MCP server:** stdio only. No HTTP. No network listener. No telemetry.
- **CI matrix:** Python 3.11 + 3.12, macOS + Ubuntu. ruff + ruff-format + mypy + pytest + coverage all gate the merge.

## See also

- `docs/ARCHITECTURE.md` — components, flows, two-layer security boundary, MCP API.
- `docs/USE_CASES.md` — five concrete personas with example flows.
- `docs/COMPARISONS.md` — engram vs Mem0 / Letta / basic-memory / Open Brain / Obsidian / engraph.
- `docs/adr/` — 7 ADRs (storage, MCP, sync, embedding, sync coordinator, multi-vault, team brain).
- `~/repos/github.com/kpachhai/idea-forge/docs/superpowers/specs/2026-05-04-engram/` — original spec (12 docs; historical authority).
- `~/repos/github.com/kpachhai/idea-forge/workspace/engram/PHASE_<N>_RETROSPECTIVE.md` — lessons learned (Phase 2-4).
