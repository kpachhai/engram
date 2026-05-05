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
  uniqueness across 2000-capture batches.
- `engram.utils.lock` - per-vault advisory lock at `<vault>/.indexes/engram.lock`
  using `fcntl.flock(LOCK_EX | LOCK_NB)` so the kernel arbitrates between
  concurrent processes attempting to serve the same vault. Diagnostic JSON
  metadata (pid, hostname, acquired_at, version) for "who holds the lock"
  reporting; cross-host vs same-host detection; `--force` override unlinks
  and retries once. Stale locks self-recover via flock semantics on FD close.
  atexit + SIGTERM/SIGINT cleanup hooks; signal handlers restored on release.
  Concurrent-process test uses subprocess.Popen so flock is exercised
  across kernel-arbitrated FDs.
- `engram.models` - Pydantic v2 boundary types:
  - `Frontmatter` model with strict validation (canonical + custom prefixes
    accepted; path-traversal, NULL, RTL-override unicode rejected; portability
    Literal-typed; tz-aware datetime enforced; 64-hex-char fingerprint
    pattern; `schema_version` defaults to 1 per NFR5; `extra="allow"` so
    unknown future fields round-trip).
  - `CANONICAL_PREFIXES` constant (15 values) and
    `DEFAULT_PORTABILITY_BY_PREFIX` (Domain and Artifact default to
    `sensitive` per BYOC).
  - `Thought` and `ThoughtWithSimilarity` runtime objects with `vault` and
    `legacy_id` fields present from v1.0 for forward compat (R29).
  - `CaptureInput`, `CaptureOutput`, `SearchInput`, `SearchOutput`,
    `ListInput`, `ListOutput`, `StatsOutput`, `FetchInput`, `FetchOutput`,
    `Filter`, `PortabilityCounts`, `SortOption` - one-to-one MCP API
    contract per `02-TECHNICAL_DESIGN.md`.
- 192 tests total at this checkpoint (109 from layer-0 + 13 lock + 70 models),
  coverage 94.15%.
- `engram.config` - five-layer config loader + Pydantic models:
  - Models: `SyncConfig`, `LLMConfig`, `VaultMount`, `UserConfig`, `VaultConfig`,
    `EffectiveConfig`. The `llm:` block is reserved per `02-TECHNICAL_DESIGN.md`
    Optional LLM-Mediated Features; Phase 1 parses but ignores it at runtime.
  - `load_config()` implements the full precedence (defaults -> per-user YAML
    -> per-vault YAML -> `ENGRAM_*` env -> CLI flags) via two-pass load:
    Pass 1 resolves the vault path from per-user config + `--config` /
    `--vault` flags; Pass 2 loads the per-vault YAML at the resolved path.
  - `resolve_default_user()` priority: CLI > env > per-user YAML > devkit
    `~/.config/devkit/identity.json` `github_username` > `$USER` > literal
    `engram-user`. Devkit identity.json is a soft dependency; absence,
    malformed JSON, and missing field all fall through silently.
  - `ensure_user_config_dir()` creates `~/.config/engram/` with mode 0700 per
    `06-SECURITY.md` Boundary B1.
  - Fatal errors with clear messages for: no vault configured, vault directory
    does not exist, `--config` file does not exist, no `vaults:` list, no
    primary vault marked, requested `--vault` not in list.
- 239 tests total now (47 new config tests), coverage 94.22%.
- `engram.storage.sqlite` - SQLite + sqlite-vec connection factory and schema:
  - Three spec-defined tables (`thoughts`, `thought_embeddings` virtual,
    `migrations`) plus a Phase 1 `engram_settings` KV table that records
    embedding model name, embedding dimension, and sqlite-vec version (Risk
    R22 mitigation; uses settings rows instead of an undocumented schema table).
  - sqlite-vec extension probe + load via `sqlite_vec.load(conn)`. Detects
    when Python's stdlib sqlite3 lacks loadable-extension support and raises
    a clear `IndexError` with a remediation pointer to uv-managed Python.
  - Dimension and model-name mismatch detection on reopen: changing the
    embedding model raises `IndexError` directing the user to
    `engram reindex --full --model <new>`.
  - Schema version tracked via `PRAGMA user_version`. WAL journal mode.
    Database file mode 0600 on POSIX.
- `engram.storage.sqlite_queries` - parameterized query helpers:
  - `insert_thought` (atomic row + optional embedding insert),
    `get_thought_row`, `list_thoughts` (returns rows + true `total_count`
    pre-pagination), `search_thoughts_by_vector` (sqlite-vec ANN with
    metadata filter; returns rows + cosine similarity in [0, 1]; pending
    rows excluded), `update_thought_metadata`, `update_thought_body`,
    `delete_thought`, `upsert_embedding`, `mark_embedding_status`,
    `list_thoughts_with_status` (for doctor --repair), `get_stats`,
    `record_migration_start`/`record_migration_complete`,
    `iter_all_thought_paths`.
  - Tag filtering uses `json_each` rather than `LIKE '%"x"%"` so adversarial
    tag names don't false-match substrings (Risk R24).
  - All SQL is parameterized; injection attempts via filter values land as
    literal strings.
- 62 new tests across the SQLite layer; total 301; coverage 92.69%.
- `engram.storage.markdown` - markdown SoT layer:
  - `read_thought()` / `write_thought()` / `split_frontmatter()` / `FrontmatterDrift` /
    `DriftReason`. Two-parse design (PyYAML safe_load for Pydantic-validated read,
    ruamel.yaml round-trip for write-side preservation of unknown extras) so
    forward-compat fields survive write+read cycles (Risk R19).
  - Frontmatter Schema Drift Handling per `02-TECHNICAL_DESIGN.md`: missing
    schema_version defaults to 1 (NFR5 exception); missing required field surfaces
    `MISSING_REQUIRED_FIELD` drift and the file is not indexed; non-UTF-8 surfaces
    `NOT_UTF8`; YAML parse error surfaces `YAML_PARSE_ERROR`; unknown prefix value
    surfaces `UNKNOWN_PREFIX` but the file IS indexed; unknown extra fields
    surface `UNKNOWN_EXTRA_FIELD` (info-level) and round-trip preserved.
  - A4 explicit test: body containing literal `---` mid-document round-trips intact.
  - Force-quote `id`, `fingerprint`, `created_at`, `updated_at` on write to avoid
    YAML scalar misinterpretation (e.g., all-hex-digit fingerprints could parse as int).
  - Atomic write via `engram.utils.atomic_write`; mode 0600.
  - Hypothesis property test for write+read body round-trip (handles CRLF
    normalization + trailing-newline append per the writer contract).
- `engram.storage.facade` - `VaultStorage` class composing markdown SoT + SQLite:
  - `capture()` implements the Flow A atomicity contract: markdown write must
    succeed before SQLite insert; embedding optional (omitted -> embedding_status
    `'pending'`); SQLite txn wraps row + embedding insert; git sync hook stubbed
    for Phase 2+. `_post_capture_sync()` is the placeholder.
  - `parse_prefix_from_content()` extracts `[Word]` from leading body content;
    falls back to `Note`. Multi-word prefixes (`[Action Item]`, `[Session Summary]`)
    are returned intact.
  - BYOC default-portability: `Domain` and `Artifact` default to `sensitive`;
    other prefixes default to `portable`. Explicit override at capture time.
  - `get_by_id()`, `list_thoughts()`, `search()`, `update_metadata()`,
    `update_body()`, `delete()`, `stats()`, `repair_pending_embeddings()`.
  - Q1 default applied: content >1 MB raises `VaultError`; >100 KB warns.
- 55 new tests (23 markdown + 32 facade); total 356; coverage 90.51%.
- `engram.embedding` - lazy-loaded FastEmbed provider:
  - `EmbeddingProvider` runtime-checkable Protocol so the storage layer never
    has to import the concrete provider.
  - `FastEmbedProvider` boots cold (~2s budget for `initialize`) and lazy-loads
    `BAAI/bge-small-en-v1.5` on first `embed()` under a `threading.Lock` so the
    first capture or search after cold start absorbs the 2-3s model-load cost
    once per process. Dimension verified against the model card on first use.
  - `aembed()` async wrapper executes the sync embed via `asyncio.to_thread`
    so the MCP event loop stays unblocked under concurrent tool calls.
- `engram.storage.reindex` - drift-aware reindex pipeline:
  - Four modes: `INCREMENTAL` (re-embed drifted bodies + insert new files),
    `FULL` (re-embed everything from markdown - used after embedding model
    swap), `REPAIR` (only `embedding_status='pending'` rows), `REMOVE_ORPHANS`
    (snapshot-guarded SQLite-only cleanup of rows whose markdown disappeared).
  - Snapshot-timestamp guard (R11): orphan removal compares against the walk
    start time so concurrent captures during reindex are not deleted.
- `engram.diagnostics.doctor` - 9-check health pass:
  - Vault directories exist, sqlite-vec is loadable, embedding settings agree
    with the `engram_settings` row, embedding model is downloaded, index +
    markdown counts agree, no orphan rows, no orphan tempfiles, no pending
    embeddings (`--repair` reconciles).
  - `CheckStatus` is one of OK / WARN / FAIL; the report's `exit_code`
    property maps to 0 / 1 / 2 so CI consumers can branch on it directly.
- `engram.cli` - Typer-driven console entrypoints registered as `engram`:
  - `engram init <vault>` scaffolds `<vault>/thoughts/`, `<vault>/.indexes/`,
    `<vault>/.engram/config.yaml` with mode 0700 directories + 0600 config.
  - `engram doctor` runs the 9-check pass and exits 0/1/2 per status.
  - `engram reindex` exposes the four modes; `--repair` and `--full` accept
    explicit model overrides.
  - `engram serve` acquires the `VaultLock`, builds the FastMCP server, and
    blocks on stdio. Cloud-sync paths (Dropbox, iCloud, Google Drive) emit
    a structured WARN per Q10 default.
- `engram.mcp` - FastMCP-wired tool surface:
  - Five `@mcp.tool` handlers (capture_thought, search_thoughts, list_thoughts,
    thought_stats, fetch) - one-to-one with the Open Brain MCP surface so
    existing prompts and skills work unchanged.
  - capture: embedding-failure is non-fatal (sets `embedding_status='pending'`,
    structured WARN logged); content >1 MB rejected with `VaultError`.
  - fetch: returns null (NOT error) for unknown id so the MCP client can
    distinguish "no row" from "tool failure".
- `engram.migration.open_brain` - one-shot Open Brain -> engram migration:
  - Six steps per `04-MIGRATION.md`: connect/probe (verifies the OB endpoint
    honors `sort=created_at_asc`), enumerate (paginated `list_thoughts`),
    transform (UUID-v7, prefix parsing, fingerprint, idempotency triple
    `(fingerprint, source, created_at)`), write (markdown + SQLite +
    embedding under Flow A), validate (random-sample byte-level fetch
    round-trip), report (`migration-report.json` + audit-trail row).
  - F1 empty corpus, F3 idempotent rerun via triple match, F5 future
    `created_at` preserved as `legacy_created_at`, F6 empty body skipped
    + error logged, F8 dry-run reads but writes nothing, F10 `--limit` caps
    at N, F12 `--prefer-legacy-id-match` in-place update for actively-edited
    sources.
  - CLI: `engram migrate-from-open-brain` with `--url` / `--key`
    (env-var-preferred per ps-aux safety), `--config`, `--vault`, `--dry-run`,
    `--limit`, `--prefer-legacy-id-match`,
    `--confirm-supabase-snapshot-taken` (refuses non-dry-run without it),
    `--report-path`.
- Test infrastructure:
  - `tests/fixtures/corpus.py` - deterministic synthetic corpus generator
    covering all 15 canonical prefixes, all 3 portability values, and
    strictly-increasing `created_at`. Same generator scales to 10K for the
    benchmark below.
  - `tests/properties/test_invariants.py` - hypothesis tests for capture/fetch
    round-trip, fingerprint stability under whitespace + line-ending
    normalization, `search` top-k upper bound, and incremental reindex
    idempotency.
  - `bench/search_10k.py` - NFR1 measurement harness over 10K synthetic
    thoughts with a deterministic stub embedder. Local p95 ~37ms (target:
    <100ms). Exits non-zero when the threshold is exceeded so CI can wire
    it directly.
- ADRs at `docs/adr/`: 001-storage-recipe (markdown + SQLite + sqlite-vec),
  002-mcp-tool-surface (five-tool surface, frozen for v1.x),
  003-sync-model (git CLI, no library, Phase 2+),
  004-embedding-model (BAAI/bge-small-en-v1.5 via FastEmbed).
- Final test count for the Phase 1 cut: 433 tests across 18 modules (config,
  diagnostics, embedding, mcp, migration, models, storage, utils, fixtures,
  properties).

[Unreleased]: https://github.com/kpachhai/engram/compare/...HEAD
