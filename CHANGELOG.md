# Changelog

All notable changes to engram will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The MCP tool surface is committed-stable for the v1.x lifetime per the API stability commitment in `02-TECHNICAL_DESIGN.md`.

## [Unreleased]

### Added (Phase 3 - multi-vault foundation + friend-share + optional LLM)

- **VaultRegistry** (`engram.multivault.registry`): in-process
  resolver mapping vault `name` -> open `VaultStorage` + role
  (`primary` / `read-only`) + optional sync coordinator. Mount-time
  realpath collision check (canonical enforcement point per ADR
  006 D1). At-most-one-primary invariant + read-only-vault
  hard-refusal flag plumbing.
- **Cross-vault aggregator** (`engram.multivault.aggregator`):
  `aggregate_search` with portability push-down at the per-vault
  Filter (`block` NEVER returned regardless of any flag);
  per-vault floor (default 3); ATTACH-vs-SEQUENTIAL mode at the
  11-vault threshold; `degraded_vaults` list for per-vault
  timeout. Embedding-model compatibility check on every cross-vault
  invocation.
- **Defense-in-depth portability gate**
  (`engram.multivault.portability`): `assert_no_block_in_results`
  + `strip_block_thoughts` + `split_portabilities`.
- **Bundle export / import**
  (`engram.bundle.{format,exporter,importer}`): on-disk format is
  `manifest.json` + `thoughts/<rel>.md` in a streaming tar.gz.
  Per-file 1 MB / per-bundle 4 GB caps; manifest written LAST;
  importer enforces NFC + path-traversal refusal + YAML safe-load
  + `portability=block` filter + id-collision pre-flight (atomic)
  + `bundle_id`-chain cycle detection.
- **`engram export` / `engram import` CLI** (`engram.cli.bundle`):
  default portability filter is `portable` only; `--portability` is
  repeatable; refuses while serve holds the per-vault lock;
  `--allow-read-only` opt-in.
- **LLM provider abstraction** (`engram.llm`): five adapters
  (Anthropic / OpenAI / Ollama / llama.cpp / OpenAI-compatible) +
  MockProvider for tests + `resolve_provider` per-thought
  portability gate. `base_url` validated against
  `~/.config/engram/trusted-llm-urls.yaml`.
- **`LLMBudget`** (`engram.llm.budget`): per-day cost cap persisted
  to `<primary>/.indexes/llm_usage.json`; `truncate_to_budget`
  preserves per-vault floor.
- **Citation post-validator** (`engram.llm.citations`): strips
  hallucinated UUID-shaped citations from LLM responses.
- **`summarize_thought` + `synthesize_thoughts` MCP tools**
  (`engram.mcp.llm_tools`): per-thought summary + cross-vault RAG
  with default-off friend-vault inclusion (B-4 fix), anti-injection
  delimiter wrap + system prompt, citation post-validation.
- **Multi-vault server** (`engram.mcp.server.build_multivault_server`):
  registry-routed 5 stable tools + 2 additive Phase 3 LLM tools.
- **Multi-vault serve startup**
  (`engram.cli.serve_multivault`): testable orchestrator for the
  Step 17 ordering.
- **Phase 3 doctor extensions** (`engram.diagnostics.phase3_checks`):
  eight new checks corresponding to the eight new Phase 3 codes.
- **Phase 3 errors** (`engram.errors`): `VaultReadOnlyError`,
  `VaultPathCollision`, `DuplicateVaultName`,
  `EmbeddingModelMismatch`, `BundleImportError`,
  `BundleCycleDetected`, `BlockThoughtLLMDisallowed`,
  `LLMProviderError`.
- **Phase 3 config**: `AggregatorConfig`, new `LLMConfig` fields,
  `UserConfig._check_one_primary_vault` validator,
  `EffectiveConfig.aggregator` + `.vaults`.
- **Documentation**: `docs/adr/006-multi-vault-and-llm.md`,
  `docs/PHASE_3_CODE_COMPLETE.md`, `docs/MULTI_VAULT_SETUP.md`,
  `docs/FRIEND_SHARE_GUIDE.md`, `docs/LLM_FEATURES.md`.

### Changed (Phase 3)

- `EffectiveConfig` now carries `aggregator` and `vaults` fields
  populated by the loader from `UserConfig`.
- `VaultStorage` accepts `read_only_role: bool`; every public write
  entry-point gates on this flag.
- `engram.diagnostics.check_codes` exposes
  `ALL_PHASE_3_CHECK_CODES` (14 Phase 2 + 8 Phase 3 = 22 codes).
- Pre-existing fingerprint property test fixed: hypothesis can
  generate mixed line-ending content; test now normalizes to
  LF-only first, then exercises the three transforms.

### Security (Phase 3)

- Per-thought portability gate at the LLM layer (`block` always
  refuses; `sensitive` requires local provider).
- Friend-vault content excluded from LLM RAG by default
  (`include_friend_vaults=False`).
- Anti-injection delimiter wrap + system prompt + citation
  post-validator.
- Bundle import gate: path-traversal, oversize, YAML safe-load,
  block filter, id-collision atomicity, cycle detection.
- Read-only vault hard refusal at storage write boundary.
- LLM `base_url` trust file (separate from main config).

### Added (Phase 2 - multi-machine sync)

- **Sync coordinator state machine** (`engram.sync.coordinator`) with 10
  explicit states (`IDLE`, `DEBOUNCING`, `COMMITTING`,
  `COMMITTED_NOT_PUSHED`, `FETCHING`, `PUSHING`, `PAUSED_FOR_MIGRATION`,
  `AUTH_REQUIRED`, `MANUAL_RESOLUTION_REQUIRED`, `DISABLED`). Allowed
  transitions encoded in `ALLOWED_TRANSITIONS`; disallowed transitions
  raise `SyncError`. Owns asyncio queue + lock + ring buffer of last
  256 events. Debounce window (default 60s) coalesces rapid captures;
  max-deferral ceiling (default 300s) flushes long bursts.
- **Typed async git wrapper** (`engram.sync.gitops`) over the Phase 1
  `run_git` helper with `GitErrorClass` classification (AUTH,
  NETWORK_TRANSIENT, NETWORK_PERMANENT, NON_FAST_FORWARD, CONFLICT,
  LOCK_HELD, UNKNOWN). Functions: `is_inside_work_tree`,
  `current_branch`, `remote_url`, `default_remote_branch`,
  `status_porcelain` (porcelain v1 -z), `ahead_behind_count`,
  `commit_paths`, `fetch`, `pull_rebase`, `push` (with
  `force_with_lease` + `set_upstream`), `verify_commit`, `git_version`.
- **Conflict marker scanner** (`engram.sync.gitops.conflict_marker_scan`):
  whole-file walker requiring BOTH `<<<<<<<` AND `>>>>>>>` markers;
  the lone hunk separator is a markdown horizontal-rule and does NOT
  trigger.
- **Per-vault identity check** (`engram.sync.identity`) reading
  `.engram/identity.local` (machine-local; gitignored). Defends
  against R-H3 cross-vault contamination by refusing to push when
  the resolved `origin` URL does not match `expected_remote_pattern`.
- **R-M9 reflog gate**: before any pull-rebase, the coordinator
  captures the previous `origin/<branch>` SHA, fetches, then asserts
  reachability via `git merge-base --is-ancestor`. If unreachable
  (force-push detected upstream), refuses to auto-rebase and
  transitions to `MANUAL_RESOLUTION_REQUIRED`. `--force-with-lease`
  is the only force semantics ever invoked.
- **MigrationLock** (`engram.utils.lock.MigrationLock`): separate
  flock from `VaultLock`; `MigrationLock.is_held()` cross-process
  probe. Coordinator parks in `PAUSED_FOR_MIGRATION` while migration
  is running, resumes on release.
- **14 startup probes** (`engram.sync.startup_probes`) mapping 1:1
  to doctor check codes: git version floor, autocrlf drift, LFS
  drift on `*.md`, branch alignment, submodules under `thoughts_dir`,
  remote default-branch match, gitignore required entries
  (`.indexes/` + `*.sqlite*`), cloud-sync under `.git/`, GPG agent
  reachable, vault identity remote match (R-H3), per-vault user
  identity (R-M14), working-tree-dirty at startup (R-M12),
  read-only role contradicts auto-push, signed commits required.
  Per-cycle re-runs of probes 7 + 11 catch mid-session admin changes.
- **`engram clone-vault <url> <local_path>`** (Step 14 / R-H1):
  `git clone --no-checkout` -> delete `.git/hooks/` -> `git checkout`
  so a malicious post-checkout hook in the remote never executes.
  Writes a starter `.engram/identity.local` template.
- **`engram sync`** subcommand with `--pull` / `--push` /
  `--first-push` / `--resume` (default = pull-then-push). Refuses
  to run while the per-vault flock is held by `engram serve`.
- **`engram sync compact`** quarterly maintenance: `git gc --auto`
  + `gc.reflogExpire=30.days.ago` (L2/L3 mitigation).
- **`engram serve` startup ordering** (Step 17): startup probes
  before lock; conflict-marker scan after lock; coordinator built +
  attached to `VaultStorage`; drain on shutdown.
- **14 doctor sync checks** (`engram.diagnostics.doctor.run_sync_diagnostics`)
  reusing the probe logic. Non-git vaults emit OK rows for every
  Phase 2 code rather than failing checks that do not apply.
- **ADR 005** documenting the state machine + cross-vault contamination
  guard + force semantics + conflict-marker handling.
- **`docs/MULTI_MACHINE_SETUP.md`**: operator-facing setup guide.
- **`docs/PHASE_2_CODE_COMPLETE.md`**: Phase 2 exit-criteria validation
  paralleling the Phase 1 doc.

### Changed

- `engram.config.SyncConfig` extended with 11 new Pydantic-validated
  Phase 2 fields: `role` (`primary` | `read-only`), `disabled`,
  `debounce_window_seconds`, `max_deferral_seconds`,
  `push_retry_count`, `push_retry_backoff_seconds`,
  `push_timeout_seconds`, `allow_unsigned`, `use_no_verify`,
  `signed_pull_required`, `expected_remote_pattern`.
- `engram.storage.facade.VaultStorage` gains an optional
  `_sync_coordinator` attribute and `set_sync_coordinator()`
  injection point. `_post_capture_sync` forwards
  `thought.file_path` to `coordinator.enqueue` when attached;
  unit tests stay hermetic by leaving the coordinator unset.

### Security

- R-H1: `clone-vault` deletes hooks BEFORE checkout. Verified by
  test that plants a malicious `post-checkout` hook in the bare
  source and asserts the sentinel file is never written.
- R-H3: `vault_identity_remote_match` probe refuses pushes to a
  remote URL that does not match the per-vault identity pattern.
- R-H6: `conflict_markers_present` doctor + startup check refuses
  to serve a vault containing literal merge markers.
- R-H7: `cloud_sync_under_dotgit` probe FAILs vaults whose `.git/`
  resolves under a known consumer cloud-sync provider.
- R-M9: reflog gate refuses to auto-rebase across an upstream
  history rewrite; the operator must intervene manually.

### Added (Phase 1)

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
