# engram Phase 1 - Implementation Plan

**Status**: v2 (post-critique revision)
**Authored**: 2026-05-04
**Source spec**: `idea-forge/docs/superpowers/specs/2026-05-04-engram/` (11 docs, ~26K words)
**Authoring method**: `superpowers:deep-plan` (3 sub-agents: code-analysis, risk-id, edge-cases; one critique pass with `code-reviewer`; one revision pass)

This plan breaks Phase 1 into ordered tasks with explicit dependencies, deliverables, acceptance criteria, and test scenarios. Phase 1 is the only thing in scope; Phases 2-6 are out of scope.

---

## Goal

When complete, all five conditions are simultaneously true:

1. `pip install engram-mcp-server` succeeds in fresh virtualenvs on macOS (Intel + Apple Silicon) and Linux x86_64.
2. All 5 MCP tools (`capture_thought`, `search_thoughts`, `list_thoughts`, `thought_stats`, `fetch`) match the `02-TECHNICAL_DESIGN.md` MCP API Contract byte-for-byte and function end-to-end with Claude Code as the client.
3. `engram migrate-from-open-brain` against the maintainer's actual Open Brain corpus completes with zero errors and 100% deterministic round-trip on the 10-thought sample (via `fetch(id)` byte-for-byte after engram's body normalization, NOT semantic search).
4. All Phase 1 exit criteria from `03-ROADMAP.md` Phase 1 Exit Criteria are explicitly verified item by item, with evidence captured in a ship-readiness checklist.
5. `ruff format --check` + `ruff check` + `mypy --strict` + `pytest` (+ ≥80% coverage) + `pre-commit run --all-files` all green on Python 3.11 and 3.12 across macOS and Ubuntu runners; `engram doctor` all-green on a fresh install AND on the migrated vault.

The plan tracks two milestones:

- **Code-Complete (Step 20)**: deliverables 1-11 implemented and CI green. The maintainer can `pip install` and use engram.
- **Shipped (Step 21)**: Code-Complete plus the 14-day daily-driver trial (per `03-ROADMAP.md` Phase 1 Exit Criteria bullet 6). Phase 1 is not "Shipped" until both hold; this distinction matters because Step 20's hand-off begins the trial period, not ends it.

---

## Current State

(From code-analysis sub-agent.)

**Already in scaffold** at `<repo>/`:

- Tooling-first foundation per `10-CODE_QUALITY.md`: `pyproject.toml` (PEP 621 + ruff + mypy strict + pytest config), `.pre-commit-config.yaml`, `.github/workflows/ci.yml` (Python 3.11+3.12 × macOS+Ubuntu), Apache-2.0 LICENSE, README/CHANGELOG/CONTRIBUTING shells, `.python-version`, `uv.lock`.
- Minimal package: `src/engram/__init__.py` (version), `src/engram/cli/__init__.py` (Typer app shell with `--version`), `src/engram/py.typed`.
- Smoke tests: 3 tests covering version + CLI (88.24% coverage; >80% threshold met). All quality gates green.
- Runtime deps declared: `fastmcp>=2.0`, `fastembed>=0.7`, `sqlite-vec>=0.1.6`, `pydantic>=2.7`, `pydantic-settings>=2.4`, `structlog>=24.1`, `typer>=0.12`, `ruamel.yaml>=0.18`, `httpx>=0.27`, `uuid7>=0.1`, `onnxruntime>=1.17,<1.21` (Intel Mac wheel pin).

**Spec'd but not built** (Phase 1 deliverables 2-9 + 11):

- All `engram.storage`, `engram.embedding`, `engram.sync`, `engram.mcp`, `engram.migration`, `engram.errors`, `engram.utils`, `engram.config`, `engram.models` modules.
- All CLI subcommands: `init`, `serve`, `reindex`, `doctor`, `migrate-from-open-brain`.
- 100-thought fixture corpus (synthetic only; three-repo data ownership rule).
- 10K-thought synthetic benchmark + perf tests.
- ADRs in `docs/adr/`.
- Property-based tests (hypothesis) for storage + embedding invariants.
- E2E tests spawning `engram serve` as subprocess and exercising MCP over stdio.

**Open Brain source schema** (from `<your-persistent-memory-repo>/open-brain/schema.sql`): 7 columns - `id` (UUID-v4), `content` (text), `embedding` (vector(1536)), `metadata` (jsonb), `created_at`, `updated_at`, `content_fingerprint` (lowercased+whitespace-collapsed SHA-256). Migration recomputes both fingerprint (engram's normalization) and embedding (engram's 384-dim model). Source UUID-v4 always becomes `legacy_id`; engram mints fresh UUID-v7.

---

## Risks

(From risk-id sub-agent. Each High/Medium item has a mitigation step in the Plan; step references re-audited post-critique to match the renumbered plan.)

### High severity

| ID | Risk | Mitigation step |
|---|---|---|
| R1 | Markdown lands on disk but SQLite txn fails → silent index drift | Step 10 (storage facade) implements explicit doctor-recovery test; Step 12 (doctor) verifies repair-from-disk path |
| R2 | `INNER JOIN thoughts × thought_embeddings` drops `embedding_status='pending'` rows from search/list/stats | Step 8 (storage queries) uses LEFT JOIN with explicit pending filter at query time; pending rows excluded from search but included in list+stats |
| R3 | Atomic-write rename non-atomic across filesystems | Step 5 (atomic_write helper): tempfile created in SAME directory as destination; F_FULLFSYNC on macOS; fsync parent directory after rename |
| R4 | Cold model load timeout exceeds Claude Code's `initialize` timeout (~30s) → MCP marked broken | Step 14 (serve): `initialize` returns immediately; embedding lazy-loaded with timeout + progress logging; doctor `--download-model` documented as pre-flight; first-call latency budgeted in Step 11 tests |
| R5 | Stray `print()` to stdout corrupts JSON-RPC framing | Step 14 (serve): swap `sys.stdout` to `sys.stderr` BEFORE FastMCP imports tqdm or initializes stdout writer; e2e test asserts no garbage on the protocol channel |
| R6 | Asyncio + blocking C extensions freeze event loop | Step 11 (embedding) wraps FastEmbed calls in `asyncio.to_thread`; Step 8 wraps SQLite calls likewise; e2e concurrent-call test catches regression |
| R7 | sqlite-vec extension load + Python `sqlite3` build flag (`--enable-loadable-sqlite-extensions`) | Step 7 (sqlite layer): explicit detection at startup with helpful error pointing to uv-managed Python; doctor reports it |
| R8 | Lock file PID reuse + TOCTOU race | Step 6 (lock helper): `O_CREAT|O_EXCL` open + `fcntl.flock(LOCK_EX|LOCK_NB)` on the FD; kernel arbitrates; stale-detection only via flock not just PID check |
| R9 | Cloud-sync providers (Dropbox/iCloud/OneDrive) corrupt SQLite via incompatible byte-range locking | **Open Question Q10** for the maintainer: WARN-on-detect vs refuse-to-start-with-override. Default to WARN until the maintainer chooses. Step 14 (serve) implements per Q10 outcome. |
| R10 | Frontmatter `schema_version` missing → Pydantic strict ValidationError before NFR5 default-to-1 rule | Step 4 (models): `schema_version: int = Field(default=1)` + `model_validator(mode='before')` to handle missing-from-YAML case |
| R11 | Reindex orphan detection races with concurrent capture → just-captured row deleted by `--remove-orphans` | Step 13 (reindex): take snapshot timestamp at walk start; only consider rows older than snapshot as orphan candidates; hold write lock during orphan removal |
| R12 | F_FULLFSYNC on macOS for SoT atomic write (APFS doesn't flush to media on plain fsync) | Step 5 (atomic_write): platform-aware `F_FULLFSYNC` on Darwin via `fcntl.fcntl(fd, fcntl.F_FULLFSYNC)`; document caveat for Linux ext4 `data=writeback` |
| R13 | Round-trip validation comparing normalized vs normalized = vacuous | Step 15 (migration): comparison runs on RAW bytes from disk vs RAW bytes from OB MCP response (after MCP-frame UTF-8 decode but before engram normalization); engram-side normalization applied identically only for reporting |
| R14 | Migration pagination ties at `created_at` boundary → skipped/duplicated thoughts | Step 15 (migration): `list_thoughts(sort="created_at_asc")` with `id` tiebreak detection in client-side post-processing; cross-page duplicate IDs detected and deduped via the triple-match |
| R15 | UTF-8 decoding mid-pagination aborts entire migration loop | Step 15 (migration): per-thought try/except; bad-row skip + log + report; continue pagination |
| R16 | Path traversal via crafted prefix or slug | Step 4 (models): Pydantic `prefix` validator restricts to canonical 15-prefix vocabulary OR safe pattern; Step 6 (file_naming): slug sanitization explicitly rejects `..`, NUL bytes, RTL Unicode, absolute paths |
| R17 | Subprocess injection via commit message containing user-controlled content | Step 6 (run_command helper): always list-form, `shell=False` enforced in helper signature; commit-message variables validated against tight regex (UUID only, prefix-vocab only) |
| R18 | OB MCP key in argv exposed via `ps aux` | Step 15 (migration CLI): primary path is `OPEN_BRAIN_KEY` env var or `~/.config/devkit/references.json`; `--key` flag accepts only a sentinel `env:VAR_NAME` form (raw value is rejected with a clear error and a pointer to the env-var path) |
| R19 | ruamel.yaml `CommentedMap` ≠ Pydantic strict `dict` | Step 9 (markdown): two-parse path - PyYAML `safe_load` for Pydantic validation, ruamel.yaml round-trip for write-side preservation; reconcile on write |
| R20 | pydantic-settings 5-layer custom_settings_sources ordering + circular vault-path-resolution | Step 4 (config): two-pass load - pass 1 resolves vault path from per-user + CLI; pass 2 loads everything else with vault path known |
| R21 | FastMCP 1.x vs 2.x API divergence | Pin `fastmcp>=2.0,<3.0` (already in pyproject.toml); Step 14 (serve) uses 2.x API explicitly; ADR 002 captures the decision |
| R22 | sqlite-vec virtual table syntax migrations across versions | Step 7 (sqlite): use SQLite `PRAGMA user_version` plus a single-row settings table tracking `sqlite_vec_version` + `embedding_model_name` + `embedding_dim`. NOT a new schema-defining table; falls within "schema additions are permitted at startup migration time" per the spec's forward-compat posture. |

### Medium severity

| ID | Risk | Mitigation step |
|---|---|---|
| R23 | `total_found` field reports `len(results)` instead of true total | Step 8 (queries): explicit COUNT(*) for total; integration test asserts `total_found > k` when matches exceed page size |
| R24 | Tag filter via `LIKE '%"x"%'` false-matches substrings | Step 8 (queries): `json_each` for tag matching; property test with adversarial tag names |
| R25 | `embedding_status='pending'` rows poison stats inconsistently | Step 8 (queries): `thought_stats` documents which counts include/exclude pending; `total_count` includes all (matches markdown count); search excludes pending |
| R26 | UUID-v7 last-12-hex collision silently overwrites filename | Step 6 (file_naming): markdown write uses `O_CREAT|O_EXCL`; collision triggers regeneration |
| R27 | Prefix mismatch between frontmatter and body bracket | Step 13 (reindex): drift detection logs WARN about mismatch; no auto-fix (per **Open Question Q5**) |
| R28 | macOS-x86_64 wheel pinning silently bypassed by transitive dep upgrade | `uv.lock` committed (already done); CI pins resolver via lockfile; document in CONTRIBUTING |
| R29 | FastMCP tool-output Pydantic model strips fields not declared | Step 14 (server): output models declare `vault: str | None` and `legacy_id: str | None` even though Phase 1 always returns `'default'` and `None` respectively (forward-compat for Phase 3+) |
| R30 | Pre-commit mypy uses different env than uv-managed → false positives | Step 1 (logging) sub-bullet: validate `.pre-commit-config.yaml` `additional_dependencies` matches the runtime dep set; CI runs both `uv run mypy` AND `pre-commit run mypy` to catch divergence |

---

## Edge Cases

(From edge-cases sub-agent, condensed. Each addressed in a Plan step; cross-references re-audited post-critique.)

### Storage / fingerprint (addressed in Steps 5-9)

- A1 Empty body → fingerprint `e3b0c4...855`; capture succeeds. (Step 9)
- A2 Whitespace-only body → normalizes to empty; same fingerprint as A1. (Step 9)
- A3 Body containing only `[Prefix]` → slug fallback to `thought`; fingerprint includes the bracket. (Step 6 + Step 9)
- A4 Body with literal `---` mid-document → frontmatter parser closes on first match AFTER the opening fence; later `---` lines are body content; preserved verbatim with explicit round-trip test. (Step 9)
- A5 CRLF/CR/LF normalization → identical fingerprints; rewrite normalizes to LF. (Step 9)
- A6 Slug fallback to `thought` when first 30 chars all non-alphanumeric. (Step 6)
- A7 1000+ captures in same UTC second remain unique via 12-hex tail. (Step 6)
- A11 Two files with same `id` → ERROR, neither indexed (PK enforces). (Step 9)
- A14 Atomic-write tempfile orphan after crash → reported by doctor; safe to remove. (Step 12)

### Frontmatter schema drift (addressed in Step 9)

- A8 Unknown prefix value → log WARN, INDEX with literal value, treat as custom for filtering.
- A9 Missing required field → log WARN, do NOT index. EXCEPTION: missing `schema_version` → treat as 1 and index.
- A10 Unknown extra field → preserve verbatim on next write.
- A13 Non-UTF-8 body → reject capture (boundary), log error on read.

### MCP tool boundaries (addressed in Step 14)

- B1 No content size cap (**Open Question Q1**).
- B2/B3 `k=0` and `k>100` (**Open Question Q2**; default to error per typical contract).
- B4 `offset > total_count` → empty results, correct total_count.
- B6 `fetch(id)` for non-existent id → returns `null`, not error.
- B7 `thought_stats` on empty vault (**Open Question Q3**).
- B8 `tags=[]` filter (**Open Question Q4**).

### Embedding (addressed in Step 11)

- C1 Cold-start lazy load 2-3s (per NFR1 explicit allowance); first-call latency test asserts <5s budget.
- C2 Embedding fail → `embedding_status='pending'`; capture still succeeds; recoverable via doctor.
- C3 Model dimension change → refuse to serve; require reindex --full --model.
- C4 FastEmbed model file SHA-256 mismatch → refuse to load (per `06-SECURITY.md`).
- C5/C6 Empty/multilingual/code-only bodies → all valid.

### Configuration (addressed in Step 4)

- D1 No config files → fatal with explicit error.
- D2 Empty `vaults:` list → fatal.
- D3 Multiple vaults no primary → Phase 1 has only one vault by spec; this guard is plan-deferred to Phase 3 unless a Step 4 design discussion reveals it's free.
- D4 5-layer precedence verified across CLI/env/vault/user/defaults.
- D5 `identity.json` malformed → fall back to `$USER`.
- D6 Vault path doesn't exist → fatal.
- D7 `--config` non-existent file → fatal.

### Concurrency / locking (addressed in Step 6)

- E1 Two simultaneous `engram serve` → first wins, second exits non-zero with structured error.
- E2 PID reuse confounds stale-detection → flock-based locking removes the hole (R8).
- E3 Cross-host lock → refuse with `--force` override.
- E4 User `rm`s lock mid-serve (**Open Question Q6**).
- E5 SIGTERM/SIGINT cleanup; SIGKILL recovery via stale-detection on next start.

### Migration (addressed in Step 15)

- F1 Empty OB corpus → no-op exit 0.
- F2 Network drop → resume with `--append`. Mechanism: re-run re-enumerates from offset 0 with the same `sort` param; the per-thought triple-match check skips already-migrated thoughts (the same path that powers F9 idempotency).
- F3 Triple-match dupes → migrate first, skip rest.
- F4 Same fingerprint, different source/created_at → keep both, preserve legacy_id.
- F5 Future `created_at` → use now(), preserve in `legacy_created_at`.
- F6 Empty body → skip + log + error count.
- F7 Unknown prefix (**Open Question Q7** - spec contradiction).
- F8 `--dry-run` reads but writes nothing.
- F9 Idempotent re-run → all skipped.
- F10 `--limit=0` (**Open Question Q8**).
- F11 Non-UTF-8 body → skip + log.
- F12 `--prefer-legacy-id-match`: when set, the per-thought lookup tries `(legacy_id, source)` against existing vault rows BEFORE the triple-match. On match: refresh body fingerprint, advance `updated_at`, re-embed, UPDATE the existing row in place (no new file/UUID). On no match: fall through to triple-match. (Step 15)

### Reindex / drift (addressed in Step 13)

- G1 External body edit → re-embed + update SQLite.
- G2 External metadata-only edit → update SQLite, no re-embed.
- G3 Markdown deleted externally → orphan logged; deleted only with `--remove-orphans`.
- G4 Same `id` in two files → covered by A11.
- G5 Reindex 10K under 5min → benchmark in Step 18.
- G6 Clock-skew tolerance ≤1s on `updated_at`.

### Doctor (addressed in Step 12)

- H1 All green on fresh install.
- H2 Model not downloaded → reports + `--download-model` resolves.
- H3 sqlite-vec missing → fatal with diagnostic.
- H4 Git remote unreachable → WARN, doctor still exit 0 (Phase 2+).
- H5 Index-vs-disk inconsistency → reported, repair available.
- H6 Embedding dim mismatch → reports + reindex --full instructions.

### Forward compat (addressed in Step 9 + Open Question)

- I1 `schema_version: 2` file (**Open Question Q9** - reading higher-than-current).

---

## Plan

The plan is organized into 8 layers, building from foundational utilities up to validation. Each step has an inline verifier (`-> verify:`). Tests are written FIRST (TDD per `superpowers:test-driven-development` skill). Each step ends with the full quality gate sequence run locally before moving on.

### Layer 0 - Foundations (no engram-specific behavior; pure utilities)

**Step 1 - Logging configuration (engram.logging) + pre-commit dep audit**
Configure structlog to write to stderr ONLY, with text rendering in dev and JSON in production (controlled by `ENGRAM_LOG_FORMAT` env var). Include redaction of `key`, `token`, `api_key`, `Authorization` keys. As a closely-related cleanup, audit `.pre-commit-config.yaml` `additional_dependencies` to ensure mypy under pre-commit sees the same dep set as `uv run mypy` (R30); add a CI step that runs `pre-commit run mypy --all-files` and fails if it diverges from the uv path.
-> verify: `pytest tests/test_logging.py` confirms (a) any logger call paths to stderr (no stdout writes), (b) sample log line redacts known-secret-shaped strings, (c) JSON mode produces parseable JSON. CI matrix shows pre-commit-mypy and uv-mypy both green.

**Step 2 - Custom exception hierarchy (engram.errors)**
Define `EngramError`, `ConfigError`, `VaultError`, `SyncError`, `IndexError`, `MigrationError`, `EmbeddingError`, `LockError` per `10-CODE_QUALITY.md`. Each has a stable error code attribute for MCP error mapping.
-> verify: `pytest tests/test_errors.py` confirms all classes inherit `EngramError`, each has a unique `error_code` attribute, raising via `raise X from cause` preserves `__cause__`.

**Step 3 - Pydantic models (engram.models)**

- `Frontmatter` (Pydantic v2 BaseModel): all 11 spec fields with `schema_version: int = 1` default + `model_validator(mode='before')` for missing-field-defaults-to-1 rule (R10); `prefix` validator allows canonical 15 OR custom (R16: rejects path-traversal characters); `portability` Literal-typed; `created_at`/`updated_at` `datetime` UTC-aware; `tags: list[str] = []` (factory default); `id: UUID` not `str`.
- `Thought` (the full thought object): frontmatter fields + `content: str` body + `file_path: Path`.
- `ThoughtWithSimilarity` (search result shape): adds `similarity: float`.
- All MCP tool input/output models from `02-TECHNICAL_DESIGN.md` MCP API Contract: `CaptureInput`, `CaptureOutput`, `SearchInput`, `SearchOutput`, `ListInput`, `ListOutput`, `StatsOutput`, `FetchInput`, `FetchOutput`, `Filter`. Output thought shape declares `vault: str | None = "default"` and `legacy_id: str | None = None` for forward compatibility (R29). Sort options as Literal type.

-> verify: `pytest tests/models/test_frontmatter.py` covers schema_version-missing default, prefix validation (canonical accept, unknown accept-with-warning, path-traversal reject per R16), portability Literal rejection, tz-naive datetime rejection, hypothesis property test for round-trip serialize/deserialize stability. `pytest tests/models/test_mcp_io.py` confirms output models include `vault` and `legacy_id`.

**Step 4 - Configuration (engram.config)**
`pydantic-settings` models with custom `settings_customise_sources` implementing the 5-layer precedence (R20): defaults → `~/.config/engram/config.yaml` (per-user) → `<vault>/engram.config.yaml` (per-vault) → `ENGRAM_*` env → CLI flags. Two-pass load to break circular vault-path resolution. Soft `~/.config/devkit/identity.json` integration for `default_user` fallback (per F5; falls through to `$USER` on missing field per D5).

The schema reserves an optional `llm:` block (Pydantic model: `provider`, `model`, `api_key_env`, `base_url`, `max_tokens`, `temperature`); Phase 1 parses it tolerantly and ignores it (no LLM features run). This is the spec's "designed-for, not built-yet" reservation per `02-TECHNICAL_DESIGN.md` Optional LLM-Mediated Features (line 921).

Config-directory permissions: when `engram init` and `engram serve` see `~/.config/engram/`, ensure `chmod 0700` on the directory if it exists (per `06-SECURITY.md` Boundary B1).

-> verify: `pytest tests/test_config.py` covers all 5 precedence layers, fatal errors for D1/D2/D6/D7, identity.json malformed-then-fallback (D5), env var override of YAML, presence of an `llm:` block parses without error and is ignored at runtime, `~/.config/engram/` mode is 0700 after engram touches it.

**Step 5 - Atomic write (engram.utils.atomic_write)**
Helper that writes to `<dest>.tmp` IN THE SAME DIRECTORY as `<dest>`, fsyncs the file (using `F_FULLFSYNC` on Darwin via `fcntl(fd, fcntl.F_FULLFSYNC)`), `os.replace`s to final path, then fsyncs the parent directory FD. Mode 0600 (per `06-SECURITY.md`).
-> verify: `pytest tests/utils/test_atomic_write.py` covers normal write, no orphan `.tmp` after success, leftover `.tmp` after simulated crash via `monkeypatch` of `os.replace` to raise. Property test (hypothesis): for any byte sequence, written file has identical bytes after fsync. macOS-only marker test verifies `F_FULLFSYNC` is invoked.

**Step 6 - Filename derivation, lock, run_command (engram.utils)**

- `engram.utils.file_naming.derive_filename(prefix, body, created_at, uuid7) -> Path`: implements full spec rule including slug fallback (A6), prefix-dirname normalization, last-12-hex tail. Validates against path traversal (R16): rejects `..`, NUL, RTL Unicode in any component.
- `engram.utils.lock.VaultLock` context manager: acquires `<vault>/.indexes/engram.lock` via `O_CREAT|O_EXCL` then `fcntl.flock(LOCK_EX|LOCK_NB)` (R8). Lock file content: JSON with pid/hostname/acquired_at/version. Stale recovery via `flock` failure + dead-pid check. Cleanup via context manager exit + `atexit` + signal handlers.
- `engram.utils.run_command.run(args: Sequence[str], **kwargs) -> CompletedProcess`: wraps `subprocess.run` with `shell=False` enforced (raises if anything that looks like shell syntax slips in), default `text=True`, default `check=True`, `timeout` argument required for git operations. (R17)
- `engram.utils.run_command.run_git(args: Sequence[str], cwd: Path, **kwargs) -> CompletedProcess`: thin wrapper over `run` that pre-stages the four non-interactive env vars per `02-TECHNICAL_DESIGN.md` Flow C (`GIT_TERMINAL_PROMPT=0`, `GIT_MERGE_AUTOEDIT=no`, `GIT_ASKPASS=true`, `GIT_LFS_SKIP_SMUDGE=1`). Phase 1 has no caller (sync is Phase 2), but the helper is built now so Phase 2 doesn't add a stale-design wart.

-> verify: `pytest tests/utils/test_file_naming.py` covers A6, A7 (10K-batch uniqueness), R16 path-traversal rejection, slug edge cases. `pytest tests/utils/test_lock.py` covers E1 (race), E2 (PID reuse via flock semantics), E3 (cross-host), E5 (signal cleanup). `pytest tests/utils/test_run_command.py` confirms shell injection attempts raise; `run_git` env-var staging asserted via mock subprocess.

### Layer 1 - Storage (markdown SoT + SQLite + sqlite-vec)

**Step 7 - SQLite layer (engram.storage.sqlite)**
Connection factory that loads `sqlite-vec` extension (R7: detects Python build flag via `sqlite3.connect(":memory:").enable_load_extension(True)` probe; clear error pointing to uv-managed Python on failure). Schema creation per `02-TECHNICAL_DESIGN.md` Storage Schema (`thoughts`, `thought_embeddings` virtual table, `migrations` audit table). Sets `PRAGMA user_version` to 1 (engram schema version); creates a single-row `engram_settings` KV table tracking `sqlite_vec_version`, `embedding_model_name`, `embedding_dim` (R22; this is a settings row, not a new domain table - the spec's three tables are unchanged). All queries parameterized.
SQLite database file mode 0600 (per `06-SECURITY.md` Boundary B1).
-> verify: `pytest tests/storage/test_sqlite.py` covers schema creation, sqlite-vec extension load (R7), parameterized query injection-resistance, embedding dim mismatch detection on connection (C3), SQLite file mode is 0600 on POSIX, `PRAGMA user_version=1` set.

**Step 8 - SQLite query helpers (engram.storage.sqlite_queries)**
Insert/update/select for thoughts, embeddings, migrations tables. LEFT JOIN with explicit pending filter (R2). True COUNT(*) for `total_found` (R23). `json_each` for tag filtering (R24). Cross-vault ATTACH pattern stubbed for Phase 3+ but currently single-vault.
-> verify: `pytest tests/storage/test_sqlite_queries.py` covers R2 (pending row inclusion in list+stats, exclusion from search), R23 (true total), R24 (no tag substring false-match), B4 (offset overflow), filter combinations (B5).

**Step 9 - Markdown layer (engram.storage.markdown)**
Read: PyYAML `safe_load` for Pydantic-validated parsing, ruamel.yaml round-trip for write-side preservation (R19). Frontmatter Schema Drift Handling (A8/A9/A10/A11/A13/A14). Body extraction post-`---` (A4: parser closes on first `---` after the opening fence; subsequent `---` lines are body content).
Write: ruamel.yaml round-trip serialization preserves unknown extras (A10); atomic_write for the actual file write; mode 0600.
Fingerprint: SHA-256 over normalized body per the Canonical Fingerprint Definition.
-> verify: `pytest tests/storage/test_markdown.py` covers all A* edge cases. EXPLICIT TEST for body containing `\n---\nstuff after\n` round-tripping intact (S16). Property test (hypothesis): write-then-read round-trip preserves all known fields; extra unknown fields preserved; fingerprint stable across whitespace-equivalent bodies.

**Step 10 - Storage facade (engram.storage.facade)**
Single class `VaultStorage` composing markdown + sqlite layers. Implements the capture atomicity contract (R1): markdown.write → embedding (failure-tolerant; ok/pending/failed status) → sqlite.upsert in single txn → optional git commit (Phase 2+ stub). `embedding_status='pending'` rows trackable.
-> verify: `pytest tests/storage/test_facade.py` covers R1 (markdown lands but SQLite raises → markdown survives, doctor recovers), R10 (schema_version missing read), R26 (filename collision regenerates UUID), atomicity-contract integration test.

### Layer 2 - Embedding

**Step 11 - FastEmbed wrapper (engram.embedding.fastembed + engram.embedding.model_hashes)**
`model_hashes.py`: per-file SHA-256 manifest for `BAAI/bge-small-en-v1.5` (model.onnx, tokenizer.json, config.json, special_tokens_map.json, vocab.txt). Loaded via `importlib.resources` to prevent path-shadowing.
`fastembed.py`: lazy-load wrapper. `__init__` is fast; first `embed()` call loads the model. Verifies each cached file's SHA-256 against the manifest (C4). Provides `embed(text: str) -> list[float]` (sync) + async wrapper that uses `asyncio.to_thread` (R6). Detects dimension mismatch on init (R2/C3). Treats per-file mismatch as fatal load error.
-> verify: `pytest tests/embedding/test_fastembed.py` (marker: `integration`) covers cold load, hash verification, R6 (async wrapper does not block event loop in concurrent test), C3 (dim mismatch raises). EXPLICIT first-call latency test: `__init__` returns in <100ms; first `embed()` returns in <5s; subsequent `embed()` calls return in <200ms (S7/C1). Property test: same input → same vector (within float tolerance). Test corpus also includes empty/emoji/multi-language inputs (C5/C6).

### Layer 3 - Doctor (separate from Reindex per critique B2)

**Step 12 - Doctor command (engram.cli.doctor)**
Reports: config valid (D1-D7), thoughts dir RW, index dir RW, SQLite opens, sqlite-vec loads (H3), embedding model loads (H2; `--download-model` triggers download with hash verification), git remote reachable (H4 Phase 2+; soft warn), index/disk consistency (H5/G3), embedding dim consistency (H6), orphan tempfiles in `thoughts/<prefix>/` (A14). Read-only by default; `--repair` triggers Step 13 reindex --repair behavior. Exit codes: 0 = all green; 1 = warnings (degraded but operational); 2 = errors (refuse to serve).
-> verify: `pytest tests/cli/test_doctor.py` covers H1-H6 + A14 + the three exit-code paths. Doctor on a fresh-init vault is all-green; doctor on a pre-corrupted fixture vault reports each documented drift case.

**Step 13 - Reindex command (engram.cli.reindex)**
Implements `02-TECHNICAL_DESIGN.md` Flow D. Modes: incremental (default; matches startup drift detection - body+metadata drift checks per G1/G2/G6), `--full` (drops + recreates SQLite index from markdown), `--repair` (regenerate `pending` embeddings only), `--repair --remove-orphans` (delete SQLite rows whose markdown file no longer exists). Snapshot timestamp at walk start; orphan removal holds the write lock (R11) and only considers rows older than the snapshot.
-> verify: `pytest tests/cli/test_reindex.py` covers G1/G2/G3/G6 + R11 (concurrent capture during orphan detection does not get deleted) + `--full` rebuild correctness on the 100-thought fixture corpus.

### Layer 4 - MCP server runtime

**Step 14 - MCP server + 5 tools + serve CLI (engram.mcp.server, engram.mcp.tools, engram.cli.serve)**
FastMCP-based stdio server. Lifecycle: acquire VaultLock → load config → init storage facade → init embedding (lazy actually-load on first call, but the layer is instantiated) → register 5 tool handlers → start stdio loop.

CRITICAL: `sys.stdout = sys.stderr` redirect set BEFORE FastMCP imports tqdm or initializes its writer (R5). FastMCP given an explicit stdout file handle reserved at process start.

Cloud-sync detection per **Open Question Q10** outcome. Default until Q10 resolves: WARN log on startup if vault path is under common cloud-sync directories.

5 tools: each is a thin wrapper. `capture_thought` triggers full atomicity flow (Step 10 facade). `search_thoughts` validates k bounds (B2/B3 per Q2). `list_thoughts` honors offset overflow (B4). `thought_stats` returns spec-compliant shape with handling for empty vault (B7 per Q3). `fetch` returns `null` cleanly (B6).

`serve` CLI is a thin front honoring `--config`, `--vault`, `--log-level`.

-> verify: `pytest tests/mcp/test_tools.py` covers all 5 tools' input/output contract per `02-TECHNICAL_DESIGN.md`. `pytest tests/e2e/test_serve.py` (marker: `e2e`) spawns `engram serve` as subprocess, sends MCP `initialize` then each tool, asserts no garbage on stdout (R5), measures cold-start `initialize` latency (NFR1: <2s).

### Layer 5 - Init CLI + Migration

**Step 15 - Init command (engram.cli.init)**
`init <path>`: scaffolds vault per F6 (creates `thoughts/`, `.indexes/`, `engram.config.yaml` template, `.gitignore` with the spec-mandated minimum lines, stub `README.md`, prefix subdirs for the 15 canonical prefixes). Refuses to overwrite an existing vault.
-> verify: `pytest tests/cli/test_init.py` covers fresh init, refuses-overwrite, .gitignore content, prefix-dir creation.

**Step 16 - Migration command (engram.migration.open_brain, engram.cli.migrate)**
HTTP MCP client over `httpx` calling Open Brain MCP endpoint. Reads `--url`/`--key` OR `~/.config/devkit/references.json` `open_brain_mcp_url` (R18: prefers env var `OPEN_BRAIN_KEY` over `--key`; raw `--key VALUE` is rejected with a clear pointer to the env var path). 6-step pipeline:

1. **Connect/Probe**: `initialize` + `list_thoughts(limit=1, sort="created_at_asc")` as the FIRST call. If OB rejects the `sort` parameter, surface a clear error naming the parameter and abort BEFORE enumeration starts (B4 verification gate). If it succeeds, log "OB endpoint accepts deterministic pagination."
2. **Enumerate**: paginate `list_thoughts(limit=500, offset=N, sort="created_at_asc")` (R14). Client-side post-process detects pagination ties (same `id` appearing on adjacent pages); when detected, trigger triple-match dedupe verification.
3. **Transform** per thought (per-thought try/except; UTF-8 errors skip-and-log per F11/R15): generate fresh UUID-v7 (preserve OB id as `legacy_id` always; F4); parse prefix per Q7 outcome; compute fingerprint (engram normalization); idempotency check `(fingerprint, source, created_at)` triple (F3). With `--prefer-legacy-id-match` (F12): try `(legacy_id, source)` lookup FIRST; on match, refresh body fingerprint + advance `updated_at` + re-embed + UPDATE the existing row in place (no new file/UUID); on no match, fall through to triple-match.
4. **Write** markdown (atomic) + insert SQLite (Step 10 facade); embedding lazy-fail-OK.
5. **Validate** via `fetch(id)` byte-for-byte on RAW bytes (R13).
6. **Generate** `migration-report.json` per spec schema; surface `--prefer-legacy-id-match`, `--dry-run` (F8), `--limit` (F10 per Q8), `--confirm-supabase-snapshot-taken` flags. INSERT into the `migrations` audit-trail table at run start (`started_at`); UPDATE on completion (`completed_at`, `thought_count`, `error_count`, `report_path`) - per `02-TECHNICAL_DESIGN.md` Storage Schema migrations table (S4).

-> verify: `pytest tests/migration/test_open_brain.py` (marker: `integration`) uses a mock OB MCP HTTP server (httpx mock). Covers F1-F12 edge cases including `--prefer-legacy-id-match` in-place update path. End-to-end test against the maintainer's actual OB corpus is the Phase 1 ship gate (Step 20). Test asserts the `migrations` audit row exists post-run.

### Layer 6 - Tests, fixtures, benchmarks, docs

**Step 17 - 100-thought fixture corpus (tests/fixtures/)**
Synthetic (no real user data per the three-repo data ownership rule): generator script produces 100 markdown files spanning all 15 canonical prefixes, varied portability, varied lengths (some 50-char, some 5KB), some with tags, some with multilingual content, some at clock-edge timestamps. Output deterministic (seeded random) so tests are reproducible.
-> verify: `pytest tests/test_fixture_corpus.py` confirms determinism (same seed → same files), all prefixes represented, no PII patterns (regex check), files load via storage layer cleanly.

**Step 18 - Property tests + 10K-thought benchmark (tests/property/, bench/)**
Hypothesis property tests (the 4 invariants per `10-CODE_QUALITY.md` line 294-299):

- Fingerprint stability across whitespace-equivalent bodies.
- Capture-then-fetch round-trip identity.
- Search returns at most k results.
- Reindex idempotency: `reindex; reindex` produces no-op the second time.

Benchmarks (`bench/`): scripts that build a 10K-thought synthetic corpus (variant of Step 17 generator scaled up) and measure NFR1 targets:

- Capture <200ms p95 (warm).
- Search top-10 over 10K <100ms p95 (warm).
- MCP cold start `initialize` <2s.
- Reindex 10K <5min (including model load).

CI runs a smaller (1K-thought) benchmark on every push for regression detection; the full 10K benchmark is run locally before ship and on schedule (weekly cron in CI).

-> verify: `pytest tests/property/` passes with default 100 hypothesis examples. `python -m bench.run_all` produces a JSON report with measured p95 latencies; the report is committed under `bench/results/` for trend tracking.

**Step 19 - ADRs + README + CHANGELOG entries (docs/adr/, README.md, CHANGELOG.md)**
Four ADRs per spec mandate (`03-ROADMAP.md` line 26):

- 001-storage-recipe.md: markdown SoT + SQLite + sqlite-vec; rationale and rejected alternatives.
- 002-mcp-tool-surface.md: 5-tool OB-compatible surface; API stability commitment; FastMCP 2.x choice (R21).
- 003-sync-model.md: system git CLI for Phase 2+; rejected libgit2; non-interactive env vars.
- 004-embedding-model.md: BAAI/bge-small-en-v1.5 via FastEmbed; per-file SHA-256 verification; dimension migration path.

Other architectural decisions (config precedence, locking, fingerprint canonicalization) are captured by the spec docs themselves (`02-TECHNICAL_DESIGN.md`) and referenced from the codebase via module docstrings; they do not get standalone ADRs to avoid duplication.

README.md: replace stub with full quickstart; link to ADRs; sample `engram.config.yaml`.
CHANGELOG.md: 1.0.0 entry summarizing Phase 1 deliverables.

-> verify: `markdownlint docs/adr/*.md` passes (style); each ADR has Status/Context/Decision/Consequences sections; README "Install + 5-min quickstart" actually works on a fresh checkout; CI builds docs without warnings.

### Layer 7 - Validation + ship

**Step 20 - Phase 1 Code-Complete validation (release/PHASE_1_CODE_COMPLETE_CHECKLIST.md)**
Walk each spec exit criterion EXCEPT bullet 6 (the 14-day trial which begins now). For each:

- Run the verification.
- Capture evidence (test output, log line, screenshot).
- Mark passed or document blocker.

Spec exit criteria addressable at Code-Complete:

1. `pip install engram-mcp-server` in fresh macOS+Linux venv → SUCCESS.
2. `engram serve` clean start with valid config → SUCCESS.
3. All 5 MCP tools end-to-end with Claude Code → manual smoke test transcript.
4. Maintainer's actual OB corpus migrates with zero errors + 10/10 round-trip → migration report + transcript.
5. `engram doctor` all-green on fresh install AND migrated vault → log output.
6. (deferred to Step 21) 14-day daily-driver use without falling back.
7. NFR1 perf targets met → benchmark JSON report.
8. NFR2 footprint targets met → `du -sh` + `ps aux` measurements.
9. Test suite passes on CI 3.11+3.12 × macOS+Ubuntu → CI run link.
10. ruff format + ruff check + mypy --strict + pytest + coverage>80% + pre-commit clean → CI run + local re-run.
11. ADRs in `docs/adr/` for major decisions → file listing.
12. README/CHANGELOG/CONTRIBUTING/public-API docstrings present → grep + manual review.

Hand-off to maintainer at Code-Complete: working `pip install engram-mcp-server` package, migration command verified against their corpus, all 11 of-12 criteria met (criterion 6 starts now).

-> verify: `release/PHASE_1_CODE_COMPLETE_CHECKLIST.md` has criterion-1-through-12-except-6 marked ✅ with evidence; criterion 6 marked "trial in progress, day 0/14."

**Step 21 - Phase 1 Ship (release/PHASE_1_SHIP_CHECKLIST.md)**
At the end of the maintainer's 14-day daily-driver trial:

- If no missing-data complaints, no search-quality regressions, no operational issues → mark criterion 6 ✅, declare Phase 1 Shipped.
- If issues surface → file as Phase 1.1 follow-up tasks; do not declare Shipped.
- Update CHANGELOG with the 1.0.0 release line and date.
- Tag the release: `git tag -s -m "Phase 1 shipped" v1.0.0`.

-> verify: `release/PHASE_1_SHIP_CHECKLIST.md` has criterion 6 marked ✅; CHANGELOG has dated 1.0.0 entry; git tag exists (signed).

---

## Open Questions

These are spec ambiguities or contradictions surfaced during planning + critique. Each needs maintainer decision BEFORE the affected step ships. Format per HANDOFF: cite specific spec sections.

**Q1 - `capture_thought` content size cap**
NFR1 implies "typical < 2KB" but no explicit cap. A multi-MB body would block the asyncio loop during embedding for tens of seconds. *Proposed default*: soft warning at 100KB, hard reject at 1MB with `CONTENT_TOO_LARGE` MCP error code. *Spec sections affected*: NFR1; F1 capture_thought signature.

**Q2 - `search_thoughts` k bounds**
Spec says `default 10, max 100`. Behavior for `k=0` and `k=101` undefined. *Proposed default*: `k=0` returns empty results with `total_found` reflecting true filter match count; `k>100` returns structured MCP error. *Spec sections affected*: 02-TECHNICAL_DESIGN.md MCP API Contract.

**Q3 - `thought_stats` oldest/newest on empty vault**
Spec types these as ISO-8601 string but doesn't specify the empty-vault behavior. *Proposed default*: return `oldest: null, newest: null` (Pydantic Optional), update spec output schema accordingly. *Spec sections affected*: 02-TECHNICAL_DESIGN.md MCP API Contract.

**Q4 - `tags=[]` filter semantics**
Empty list = "match no required tag" = match all? Or = "match thoughts with zero tags"? *Proposed default*: empty list = match all (treat identically to absent). *Spec sections affected*: 02-TECHNICAL_DESIGN.md search_thoughts filter.

**Q5 - Body bracket vs frontmatter prefix conflict**
External edit changes body `[Lesson]` → `[Pattern]` without updating frontmatter. Drift detection re-embeds but doesn't reconcile. *Proposed default*: drift detection logs WARN about mismatch; `engram doctor --repair` does NOT auto-fix (treats body bracket as informational); user resolves manually. *Spec sections affected*: 02-TECHNICAL_DESIGN.md Storage Schema.

**Q6 - User manually `rm`s the lock file mid-serve**
Spec is silent. *Proposed default*: periodic re-validation of own lock (every 60s); if lock file disappeared, recreate; if lock file replaced (different content), refuse to continue serving (degraded mode). *Spec sections affected*: 02-TECHNICAL_DESIGN.md Concurrent serve and Locking.

**Q7 - Migration unknown-prefix handling - spec contradiction**
04-MIGRATION.md Step 3 sub-step 2 says "Unrecognized or absent prefix → assign `Note`, log warning." Edge Cases table says "preserve verbatim." *Proposed reconciliation*: preserve the body bracket verbatim AND set frontmatter `prefix: <unknown-value>` (so it survives round-trip); the WARN/fallback report counts apply. Update step 3.2 wording to match. *Spec sections affected*: 04-MIGRATION.md Step 3 sub-step 2 + Edge Cases table row.

**Q8 - `--limit=0` semantics**
Spec is silent. *Proposed default*: reject with clear error "limit must be ≥ 1; use --dry-run to test connectivity without writing." *Spec sections affected*: 04-MIGRATION.md Flags table.

**Q9 - Reading higher-than-current `schema_version`**
NFR5 commits Phase 1-written files (v1) being readable by Phase 6 (vN). Reverse direction not committed. *Proposed default*: Phase 1 reads `schema_version > 1` files best-effort, indexing fields it knows, preserving unknown extras (per A10), logging WARN about future-version. *Spec sections affected*: 01-PRODUCT_SPEC.md NFR5; 02-TECHNICAL_DESIGN.md Frontmatter Schema Drift Handling.

**Q10 - Cloud-sync provider behavior (introduced post-critique)**
SQLite documentation explicitly warns that consumer cloud sync (Dropbox/iCloud/OneDrive) does not preserve byte-range locking; concurrent serves on two machines can produce corruption. The spec only warns about NFS/SMB. *Options*: (a) WARN only when path is under `~/Dropbox`, `~/iCloud Drive`, `~/OneDrive`, `~/Library/CloudStorage/`; user chooses to continue (default until Q10 resolves), (b) refuse to start without `--allow-cloud-sync` override, (c) silent, no detection. *Recommendation*: option (a) - WARN with a doc link explaining why, keep startup unblocked. *Spec sections affected*: 02-TECHNICAL_DESIGN.md Concurrent serve and Locking.

---

## Sequencing summary

```
Layer 0 (Steps 1-6) - foundations: logging+pre-commit-audit, errors, models, config, atomic_write, file_naming/lock/run_command
   ↓
Layer 1 (Steps 7-10) - storage: sqlite + sqlite-vec, queries, markdown, facade
   ↓
Layer 2 (Step 11) - embedding: FastEmbed + hash verification
   ↓
Layer 3 (Steps 12-13) - doctor + reindex (use storage + embedding) [doctor first; reindex builds on doctor's drift detection]
   ↓
Layer 4 (Step 14) - MCP server + 5 tools + serve CLI
   ↓
Layer 5 (Steps 15-16) - init CLI + migrate-from-open-brain
   ↓
Layer 6 (Steps 17-19) - fixtures, benchmarks, property tests, docs, ADRs
   ↓
Layer 7 (Steps 20-21) - Phase 1 Code-Complete validation, then 14-day trial → Ship
```

Steps within a layer can be parallelized via `superpowers:subagent-driven-development` when independent. Cross-layer dependencies are strict.

---

## Multi-session execution

Phase 1 is multi-session work. Each session resumes by:

1. Reading `docs/PHASE_1_PLAN.md` (this file).
2. Checking the CHANGELOG `[Unreleased]` section for what shipped previously.
3. Picking the next Layer/Step in the sequencing summary that has not been completed.
4. Following the TDD discipline for that step.
5. Running `pytest`, `ruff check`, `ruff format --check`, `mypy --strict`, and `pre-commit run --all-files` before committing.
6. Updating the CHANGELOG and audit-log at `idea-forge/workspace/engram/skill-audit-log.md`.
7. Surfacing any Open Questions answered to the maintainer; updating the plan if scope shifted.

The `dev-orchestrator` skill (global) can read this plan + CHANGELOG to recommend the next step at session start.

---

## Plan revision history

- v1 (2026-05-04) - initial draft via `superpowers:deep-plan` (3 sub-agents).
- v2 (2026-05-04) - post-critique revision: addressed 4 Blocking findings (B1 `--prefer-legacy-id-match` semantics added to F12+Step 16; B2 doctor and reindex split into Steps 12+13; B3 `engram_metadata` table replaced with `PRAGMA user_version` + single-row settings KV table; B4 OB1 `sort` parameter probe added to Step 16 sub-step 1) and material Should-Fix items (S1 `run_git` helper with non-interactive env; S2 `llm:` block reservation in config; S3 Code-Complete vs Shipped milestone split; S4 `migrations` audit-trail population; S5 0600/0700 permissions enforced; S6 step renumbering audited; S7 first-call latency test; S11 cloud-sync downgraded to Q10; S12 4 ADRs not 7; S13 4 property tests not 6; S14 forward-compat fields in output models; S15 pre-commit audit folded into Step 1; S16 explicit literal-`---` round-trip test).
