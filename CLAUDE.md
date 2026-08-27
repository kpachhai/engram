# CLAUDE.md

This file gives Claude Code (claude.ai/code) the context it needs to work productively on the engram repository.

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md) - keep changes compatible with both Claude Code and local AI models.

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
├── .github/workflows/ci.yml          # CI matrix (3.11-3.13 × ubuntu+macos)
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
│   ├── CONSOLIDATION.md              # report-then-action vault curation guide
│   ├── DAEMON_MODE.md                # daemon operator + migration guide (v0.5.0+)
│   ├── PUBLISHING.md                 # PyPI release procedure. Never run `uv publish` (or
│                                     #   `--index testpypi`, also a real upload) without an
│                                     #   instruction naming the version: an upload consumes
│                                     #   that number for good, and a yank does not free it.
│   ├── DEPLOYMENT_MODEL.md           # local-first thesis (why not cloud-hosted)
│   ├── adr/                          # 9 ADRs (one per major design choice)
│   └── archive/                      # historical: shipped plans + closed investigations
│       └── phases/                   # PHASE_<N>_PLAN + PHASE_<N>_CODE_COMPLETE (1-6)
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
│   ├── consolidate/                  # report-then-action vault curation (passes, apply, guards)
│   ├── migration/                    # Open Brain migration pipeline
│   ├── diagnostics/                  # engram doctor; check codes enumerated in check_codes.py
│   └── utils/                        # atomic_write, fingerprint, file_naming, lock
├── tests/                            # unit + integration + smoke (count: pytest --collect-only -q)
└── bench/                            # NFR1 search-latency benchmarks
```

engram was built from a written spec that lives in the maintainer's separate planning repo. That spec is not published and is not part of this repo, so nothing here should cite it: this repo is authoritative for what engram does, and `docs/adr/` plus `docs/archive/phases/` carry the design history a reader can actually open.

## Phase history (historical context, not active work)

engram shipped in six phases (solo MVP + Open Brain migration -> multi-machine git sync -> multi-vault/friend-share/LLM -> Team Brain -> daemon mode, v0.5.0 -> consolidation, v0.6.0), all code-complete; the record lives in `docs/archive/phases/`, `docs/adr/`, and `CHANGELOG.md`. Future work is operational dogfood and incremental polish, not phased delivery.

**Don't reintroduce "Phase N" framing in source comments.** That historical context belongs in plan / ADR / retrospective docs. Source code reads as a polished v1.0 product.

Known gate gap (2026-08-25): the vocabulary gate greps file *contents* and never matches the path, and its content pattern only matches the spaced prose form, so neither a phase-named filename nor a phase-named identifier such as `ALL_PHASE_4_CHECK_CODES` trips it - `git ls-files | grep -ciE 'phase[0-9]'` counts the tracked paths that still carry it, all of them under `tests/` now that the two shipped modules are named for what they check. The class is not closed. Until the vendored scanner reads paths too, filenames are enforced by review. Gate gaps in the vendored scripts themselves: `.githooks/README.md`.

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

- **`pyproject.toml` `authors` field** is the only place a real maintainer name is permitted in committed content (the project-attribution exception, same as `package.json` `"author"`).
- **No employer or company brand names** in source, tests, docs, ADRs, or commit messages. The pinned-invariants list, the spec, and the migration guide should all be readable by any forker without context about who built engram.
- **No hardcoded `/Users/<name>/` paths.** Examples in docs use `~/.local/share/engram/personal` or similar generic paths; tests use `tmp_path` fixtures.
- **No companion-repo paths bearing the maintainer's GitHub username.** Cross-references to other repos use generic terms (`your dotfiles`, `your meta-stack repo`, `your persistent-memory MCP`). Do not cite a path in an unpublished repo at all: a reader cannot open it, so it is a dead pointer whether or not it carries a username.
- **No GPG fingerprints, API keys, MCP URLs with secrets, or test fixtures derived from real keys** in committed content. Test GPG keys are generated in `tmp_path` per-test; OB1 migration secrets live outside the repo, in the maintainer's optional `~/.config/devkit/references.json` (machine-local, gitignored) - no contributor needs that file.

Run the Pre-Write Checklist before writing or editing any file in this repo. If candidate PII slips in, flag it before staging - active guidance over silent rewrites.

## Coding conventions

Follow the global rules in `~/.claude/CLAUDE.md` (use hyphens or semicolons instead of em-dashes, no emojis unless asked, fail-fast error handling, descriptive names, surgical changes only). Engram-specific additions:

- **Pydantic at every boundary.** All MCP I/O, all config files, all bundle manifests use Pydantic models with `model_config = ConfigDict(extra="forbid")` for inputs and `extra="ignore"` for outputs. New fields are additive only.
- **Atomic writes.** All file mutations go through `engram.utils.atomic_write.atomic_write_text` or `atomic_write_bytes` (tempfile + fsync + rename). Never write to the destination path directly. Enforced by `test_file_mutations_go_through_atomic_write`, which holds a reasoned allowlist of the remaining direct-write sites; a new one fails there.
- **Parameterized SQL.** All queries in `engram.storage.sqlite_queries` use `?`-placeholders. Never f-string user input into SQL. ruff S enforces this.
- **No shell=True.** Subprocess calls in `engram.utils.run_command` and `engram.sync.gitops` use list-form `subprocess.run(["cmd", "arg"], shell=False)`. ruff S enforces this.
- **Type-strict mypy.** `pyproject.toml` has `[tool.mypy] strict = true`. Every new function has type hints; `Any` is a code smell.
- **Per-vault locking.** Long-running serve processes acquire a `VaultLock` (advisory file lock via `fcntl.flock`) per vault. Never modify a vault from multiple processes without flock.
- **Failure-tolerant embedding.** If the embedder raises during capture, the thought is captured with `embedding_status='pending'` and `engram doctor --repair` regenerates later. Capture itself never fails because of embedding issues.

## Testing

```bash
uv sync --all-extras --dev          # install + lock deps
uv run pytest -q                     # full suite (~7 min)
uv run ruff format                   # auto-format
uv run ruff check --fix              # lint + auto-fix
uv run mypy                          # strict type-check (prints its own file count)
uv run pytest --cov=src            # coverage gate (floor: pyproject.toml)
```

Test taxonomy:

- `tests/<module>/test_*.py` — unit tests; one file per source module.
- `tests/integration/` — cross-subsystem flows (multi-vault, sync convergence).
- `tests/properties/` — Hypothesis property tests for invariants.
- `tests/test_phase4_cli_smoke.py` — **hermetic CLI smoke against the installed binary**. This catches wiring bugs the handler-level tests miss (Typer registration, argument plumbing, exit codes, --help output). Add a smoke test for every new CLI subcommand.
- `tests/team/test_phase4_exit_criteria.py` — 23-scenario integration suite covering the team-vault pinned invariants end-to-end via in-process composition.

**Tests must be hermetic.** No network calls (use `httpx.MockTransport` for HTTP-backed providers). Never touch the USER's GPG keyring: unit tests mock `gpg --list-secret-keys` output via a `subprocess` substitute; security-boundary tests (signature parsing, attribution, enrollment) instead use a real `gpg` in an ephemeral `GNUPGHOME` on a short `/tmp` path with a cert-only primary + signing subkey, skipif-gated when gpg is absent - see `tests/team/test_pre_receive_gpg_integration.py` (mock-only coverage of gpg status output shipped a P0). No cross-test SQLite reuse (every test owns its own `tmp_path`).

## Common operations

The CLI is self-documenting (`engram --help`, `engram <cmd> --help`); recipes for serve / doctor / reindex / export / import / team-vault / model-hash printing live there and in the docs. Three gotchas that are NOT obvious from `--help`:

- The MCP-based `migrate-from-open-brain` CLI is broken against real OB1 (OB1's MCP tools return human-readable text, not structured records) - use the Postgres-direct path in `docs/OPENBRAIN_MIGRATION_GUIDE.md`.
- `engram consolidate` (report) is safe beside the running daemon; `engram consolidate --apply` requires `engram daemon stop` first.
- `engram doctor` exiting 0 does not mean every check ran - a skip exits clean, and on a vault that is not a git repository seventeen of thirty-eight rows never run. Pass `--strict` (exits 3 when any row did not run) whenever a green doctor run is being offered as evidence that something works.

## When making changes

**Discipline that's been load-bearing on this project:**

1. **Wire new components into the user-facing path (CLI / `build_multivault_server` / `engram doctor`) in the same change that adds them.** Deferred wiring bit earlier delivery iterations.
2. **At exit:** hermetic CLI smoke per the Testing section above (smoke catches wiring bugs the handler tests miss); defense-in-depth per pinned invariant 4 (client-side AND server-side, where applicable); `verify-before-done` for any shipped claim.
3. **Spec-vs-implementation audit before claiming "done".** Run a sub-agent to cross-check the relevant spec doc against `src/engram/` - three independent gaps once escaped a closeout without this.

## Operational reality

- **Embedding model:** `BAAI/bge-small-en-v1.5` is pinned. The hash manifest in `src/engram/embedding/model_hashes.py` is populated; mismatched files raise `EmbeddingError`. Recompute via `engram doctor --download-model --print-hashes` after any model upgrade.
- **Index location:** `<vault>/.indexes/engram.db` (gitignored).
- **Locks + state:** `<vault>/.engram/` holds per-machine state (identity, push queue, orphan tarballs); always gitignored.
- **MCP server:** stdio at the client boundary. No HTTP. No network listener. No telemetry. From v0.5.0 onward (daemon mode), a per-vault Unix Domain Socket sits between the ``engram serve`` proxy and the daemon process that owns the vault — UDS is local IPC, not a network listener. Filesystem perms (0o600) plus ``SO_PEERCRED``/``getpeereid`` enforce same-UID access. The daemon calls ``os.setsid()`` after fork so it survives proxy exit (e.g. Claude Code session close) and does not die with the proxy's process group.
- **`delete_thought` confirmation contract:** the MCP `delete_thought(id, confirm)` tool requires `confirm` to be passed explicitly (no default). Always call once with `confirm=False` first — the response carries metadata + the first ~200 chars of the body — show that preview to the user, then call again with `confirm=True` only after explicit user approval. Each call deletes at most one thought; bulk delete-by-search is intentionally not supported via MCP. The `engram delete <id>` CLI parallels this with a typed-string (`delete`) confirmation gate and a `--dry-run` flag.
- **CI matrix:** Python 3.11, 3.12 and 3.13, macOS + Ubuntu. ruff + ruff-format + mypy + pytest + coverage all gate the merge.

## See also

- `docs/adr/` — 9 ADRs (storage, MCP, sync, embedding, sync coordinator, multi-vault, team brain, daemon mode, consolidation).
- `docs/DAEMON_MODE.md` — operator + migration guide for daemon mode (v0.5.0+).
- `docs/CONSOLIDATION.md` — report-then-action vault curation (v0.6.0+).
- `docs/archive/phases/` — the shipped delivery plans and close-out records. The per-phase retrospectives live in the maintainer's unpublished planning repo and are not readable from here.
