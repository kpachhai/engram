# Changelog

All notable changes to engram will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The MCP tool surface is committed-stable for the v1.x lifetime per the API stability commitment in `02-TECHNICAL_DESIGN.md`.

## [Unreleased]

### Fixed

- **`engram daemon stop` no longer hangs when a proxy is attached.**
  The daemon's `serve_forever` accept loop used `async with self._server:`
  whose `__aexit__` calls `Server.wait_closed()` and blocks until every
  accepted connection handler finishes. Handlers were awaiting MCP
  frames from still-open proxy streams (e.g. an active Claude Code
  session), so they could not return on their own; the daemon had to
  burn the full `shutdown_drain_seconds` budget before the cancel
  fallback fired - and in pathological cases (long-uptime daemon,
  proxy stuck mid-RPC) the total drain exceeded the CLI's 60s
  timeout, forcing `--force` (SIGKILL). Fix: cancel in-flight handler
  tasks immediately when the shutdown event fires, BEFORE exiting the
  listener context. `wait_closed()` then returns promptly and
  `_drain_and_exit` runs the budgeted cleanup. Measured graceful
  shutdown on a personal vault with an attached proxy dropped from
  ~5s (drain-budget-exhausted) / 60s+ (timeout-and-force) to ~4s
  end-to-end including the CLI's 0.2s pid-alive polling.

### Added

- **`delete_thought` MCP tool** with a mandatory two-call confirmation
  contract. Callers pass `confirm: bool` explicitly (no default). The
  first call (`confirm=False`) returns metadata + a ~200-char body
  preview without modifying the vault; the second call (`confirm=True`)
  removes the markdown file, the SQLite row, and the embedding row, and
  enqueues a git commit via the sync coordinator. Unknown ids return
  `deleted: false` with a `"Not found"` message (not an error). Tool
  count rises from seven to eight (six core + two LLM).
- **`engram delete <id>` CLI** with a typed-string (`delete`) confirmation
  gate. `--dry-run` prints the preview without modifying the vault;
  `--yes` skips the prompt (intended for CI / scripts that have already
  validated the id).
- **`ThoughtNotFoundError`** in `engram.errors` (error_code
  `thought_not_found`) — raised by `VaultStorage.delete()` when the id
  is absent. The MCP and CLI handlers shield callers from the exception
  and surface a user-visible "Not found" message instead.
- **`engram.utils.lock.serve_lock_metadata(vault_path)`** helper that
  returns the per-vault serve-lock metadata dict (or `None` if not
  held), without acquiring the underlying `flock`. Used by one-shot
  CLI mutating commands to detect a running daemon before opening a
  second SQLite connection.
- **`CaptureOutput.index_state`** field (`"ok"` | `"failed"`) on the
  MCP `capture_thought` response. Surfaces when the markdown was
  written but the SQLite index insert raised so AI clients can warn
  the operator that the thought won't appear in search/list until
  `engram reindex` runs. Markdown SoT is unchanged. Additive to the
  wire format with a safe default (`"ok"`) per the v1.x stability
  commitment - existing clients ignore the field with no behavior
  change.
- **`engram doctor` `orphan_markdown` check** that walks the markdown
  tree and counts files whose ID has no SQLite row. Surfaces as a
  WARN with up to 10 example IDs and the remediation hint to run
  `engram reindex`. Complements the existing `orphan_rows` check
  (SQLite-rows-pointing-to-missing-markdown). Catches accumulated
  drift that escaped the in-the-moment `CaptureOutput.index_state`
  signal (e.g. vaults imported from another tool, or vaults that
  ran on an older engram before the field existed).
- **`PRAGMA busy_timeout=5000` on every SQLite connection.** Rides
  out brief contention (concurrent writer / WAL checkpoint) before
  raising `SQLITE_BUSY`. Five seconds covers typical contention;
  persistent failures still surface as exceptions to the caller.
- **Retry-with-backoff on transient SQLite errors in `insert_thought`.**
  Three attempts total with exponential backoff (100ms -> 200ms).
  Retries on `SQLITE_BUSY` (5), `SQLITE_LOCKED` (6), and the
  `SQLITE_IOERR` family (10) via Python 3.11's `sqlite_errorcode`
  attribute. Logic errors (`SQLITE_CONSTRAINT`, `SQLITE_CORRUPT`,
  etc.) propagate immediately - retrying cannot help. Each retry
  emits a WARN log line so the operator can see contention bursts
  in the daemon log.
- **`engram doctor` `disk_usage` check** that surfaces volume-level
  disk pressure (root cause of the 2026-05-13 -> 2026-05-16
  silent-swallow incident). WARN at >=90% used; FAIL at >=95%.
  Stat failures surface as WARN rather than crashing the doctor
  run.

### Changed

- **`VaultStorage.delete(thought_id, *, source="api")`** now returns the
  deleted `Thought` (previously `bool`) so callers can emit a
  confirmation that includes prefix + portability without a separate
  lookup. The method emits a structured INFO log line
  (`thought_deleted id=... prefix=... portability=... fingerprint=...
  vault=... source=...`) and forwards the deleted thought to
  `_post_capture_sync` so the sync coordinator stages the git removal.
  Existing callers that ignored the return value (CLI move-thought)
  continue to work; callers that relied on the boolean falsy return
  for "not found" must catch `ThoughtNotFoundError` instead.
- **`engram delete` and `engram reindex` now refuse (exit 2) when the
  per-vault serve lock is held.** Both commands open a fresh SQLite
  connection and walk the index; running concurrently with the daemon
  can wedge the daemon's WAL handle and silently drop in-flight
  capture rows. The error message names the lock path + PID and
  directs the operator to stop the serve loop first. `engram sync`
  and `engram bundle` already had equivalent refusals; this brings the
  remaining two mutating one-shot CLIs into line.
- **`engram sync` and `engram bundle` migrated to the shared
  `serve_lock_metadata` helper** (no behavior change). Replaces the
  two bespoke per-CLI implementations of the lock check with a single
  source of truth. `engram bundle`'s refusal message now also shows
  the holder PID for parity with the other three (`sync`, `delete`,
  `reindex`); exit code (2) and lock-not-held behavior unchanged.
- **`VaultStorage.capture()` accepts an optional `on_index_failure`
  callback** (`Callable[[Thought, sqlite3.Error], None] | None`,
  default `None`). When supplied, the callback fires if the SQLite
  insert raises - the capture still returns the Thought, the markdown
  is still on disk, and the historical log-and-continue behavior is
  preserved when no callback is registered. Used by the MCP
  `capture_thought` handler to populate the new `index_state` field;
  internal callers (`move_thought`, `reindex`, migration, tests) keep
  their existing semantics with zero signature changes. Callback
  exceptions are caught and logged so they cannot mask the original
  capture outcome.

## [0.5.0] - 2026-05-13 — Phase 5: Daemon Mode (multi-session support)

The big change in v0.5.0: **N concurrent Claude Code sessions can now
attach to the same engram vault simultaneously**. Pre-Phase-5,
``engram serve`` held the per-vault advisory lock for its lifetime;
the second concurrent session against the same vault failed with
``LockError``. Phase 5 introduces a per-vault daemon process and
makes ``engram serve`` a thin proxy that auto-spawns the daemon on
first invocation. Existing MCP configurations need no edits.

### Added

- **Daemon subpackage** (``src/engram/daemon/``): per-vault UDS
  daemon, proxy client, fastmcp dispatch shim, spawn helpers,
  state file, log rotation handler.
- **``engram daemon`` subcommand group** with 4 subcommands
  (``start``, ``stop``, ``status``, ``logs``). ``engram daemon
  status --json`` provides a machine-readable status payload with
  the not-running shape per spec Amendment 7. ``engram daemon
  logs --follow`` tails the daemon's log file with inode-reopen
  semantics for rotation handling.
- **``DaemonConfig``** Pydantic model with 14 tunable fields
  (idle-shutdown, spawn-timeout, WAL-recovery grace, drain budgets,
  frame-size cap, log rotation knobs, content redaction). Full
  reference in ``docs/DAEMON_MODE.md``.
- **5 new daemon error classes**: ``DaemonError``,
  ``DaemonSpawnError``, ``DaemonConnectionError``,
  ``DaemonNotRunningError``, ``PeerCredRejectError`` — each with a
  stable ``error_code`` so MCP clients can route consistently.
- **6 new doctor check codes**: ``daemon_running``,
  ``daemon_socket_permissions``, ``daemon_socket_stale``,
  ``daemon_log_rotation_healthy``, ``daemon_uptime_excessive``,
  ``daemon_socket_path_too_long`` — surfaced via
  ``src/engram/diagnostics/daemon_checks.py``.
- **Peer-credential check** (``SO_PEERCRED`` on Linux,
  ``getpeereid`` on macOS) on every accepted UDS connection;
  belt-and-suspenders on top of the socket's 0o600 perms.
- **Hermetic CLI smoke** (``tests/test_phase5_cli_smoke.py``):
  8 new subprocess-based smokes against the installed binary,
  including a full ``daemon start --detach`` → ``status`` →
  ``stop`` round-trip.
- **ADR 008** (``docs/adr/008-daemon-mode.md``) captures the
  per-vault topology + auto-spawn + UDS rationale plus
  alternatives considered.
- **Operator + migration guide** (``docs/DAEMON_MODE.md``)
  documents the upgrade procedure, daemon lifecycle, status
  output, troubleshooting, full ``DaemonConfig`` reference, and
  the v0.4.x downgrade procedure.

### Added

- **``user_config_vault_name_mismatch`` doctor check**
  (``src/engram/diagnostics/phase3_checks.py``): WARNs when the
  ``name:`` field in ``~/.config/engram/config.yaml`` does not match
  the ``vault_name:`` in the vault's own ``engram.config.yaml``. This
  mismatch previously surfaced as an opaque ``VaultError: primary
  vault already mounted`` at daemon startup with no pointer to the
  fix. ``engram doctor`` now names the mismatch and shows the
  corrective action. ``ALL_PHASE_3_CHECK_CODES`` grows from 22 to
  23 codes.

### Changed

- **``engram serve`` default behavior**: now runs in proxy mode
  (auto-spawns a per-vault daemon and shuffles bytes between
  stdin/stdout and the daemon's UDS). The pre-Phase-5
  single-process stdio path is preserved bit-for-bit behind a new
  ``--no-daemon`` flag.
- **``VaultLock.__init__``** now accepts ``install_signal_handlers:
  bool = True``. Daemon use cases pass ``False`` so the daemon
  can own its own SIGTERM/SIGINT handler (spec Amendment 1).
- **``ServeRuntime.embedder``** type loosened from
  ``FastEmbedProvider`` to ``object`` to match engram's existing
  duck-typed embedder convention (mirrors ``serve_multivault``).
- **``cli/serve.py``** factored: ``_init_serve_runtime`` (async)
  + ``ServeRuntime`` dataclass + ``ServeInitError`` are now the
  shared entry between ``serve --no-daemon`` and
  ``engram daemon start``.
- **``DaemonConfig.idle_shutdown_seconds`` default changed from
  ``3600`` to ``30``**. The daemon now exits ~30 seconds after the
  last Claude session closes, matching the expected behavior that the
  daemon only runs while Claude is running. Users who want the old
  always-warm behavior can set ``daemon.idle_shutdown_seconds: 3600``
  (or any value, including ``0`` for never-auto-shutdown) in their
  vault's ``engram.config.yaml``.
- **``_attach_daemon_log_handler``** (``cli/daemon.py``): now calls
  ``os.setsid()`` after attaching the rotating log handler, placing
  the daemon in a new process group. Also redirects fd 1 and fd 2 to
  ``/dev/null``. Without this, SIGTERM sent to the spawning proxy's
  PGID on Claude Code session close also killed the daemon; and
  writes to the inherited stdout after the proxy exited triggered
  ``BrokenPipeError`` → ``SystemExit(1)`` in the rich console handler.
- **``DaemonServer._drain_and_exit``** (``daemon/server.py``): idle-
  shutdown timer is now cancelled as the first step of the drain
  sequence (step 0) before the listener close, preventing asyncio
  from logging "Task was destroyed but it is pending!" during loop
  teardown.
- **``VaultStorage.close``** (``storage/facade.py``): issues
  ``PRAGMA wal_checkpoint(TRUNCATE)`` before closing the SQLite
  connection. A crash that left a stale WAL/SHM behind caused
  ``SQLITE_IOERR`` on every tool call in the next daemon startup.

### Migration notes

#### Upgrading from v0.4.x → v0.5.0

No MCP config edits are required. The ``engram serve`` command your
client invokes today runs in proxy mode by default in v0.5.0 and
auto-spawns the daemon on first use. See ``docs/DAEMON_MODE.md`` for
the full upgrade procedure with verification steps.

#### Downgrading from v0.5.0 → v0.4.x

If you ever need to downgrade:

1. ``engram daemon stop`` first — v0.4.x does not know about daemon
   mode and ``engram serve`` will fail with ``LockError`` until the
   v0.5.0 daemon is gone.
2. Remove any ``daemon:`` block from ``engram.config.yaml`` —
   v0.4.x's Pydantic model is ``extra="forbid"`` and refuses
   configs with the block present.

(Closes risk M6 in the design spec.)

#### Phase renumbering

The pre-2026-05-12 roadmap's "Phase 5 — Enterprise Scaffolding" and
"Phase 6 — Enterprise Polish" are renumbered to Phase 6 and Phase 7
respectively. The new Phase 5 is daemon mode. The roadmap files in
``docs/superpowers/specs/2026-05-04-engram/`` (gitignored local
artifact) reflect the renumber.

### Spec reference

Design spec lives locally at
``docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md``
(gitignored under ``docs/superpowers/``). The execution plan
``docs/PHASE_5_PLAN.md`` is committed for traceability.

---

## [0.4.x] (Unreleased polish; pre-Phase-5)

### Added (public-release polish)

- **`engram summarize <thought-id>`** CLI — wraps the `summarize_thought`
  MCP handler so operators can invoke single-thought summarization from
  a terminal (per `01-PRODUCT_SPEC.md` F11). Honors the same provider
  resolution + portability gate + cost cap as the MCP tool.
- **`engram synthesize "<query>"`** CLI — wraps the `synthesize_thoughts`
  MCP handler with `--k`, `--vault-filter`, `--include-sensitive`,
  `--include-friend-vaults`, and `--json` flags.
- **`engram doctor --print-hashes`** maintainer flag — recomputes the
  cached FastEmbed model file hashes in manifest-ready format. Used
  after a model upgrade to refresh `engram/embedding/model_hashes.py`.
- **FastEmbed model integrity verification** is now active. The
  `BAAI/bge-small-en-v1.5` hash manifest is populated; mismatched
  files raise `EmbeddingError` and refuse to load. Empty manifests
  (for unpinned models) keep the original trust-on-first-use behavior
  with a single WARNING log.
- **`FastEmbedProvider.list_cached_files()`** + `_resolve_snapshot_dir`
  helper — handles HuggingFace's `models--<org>--<repo>/snapshots/<sha>/`
  cache layout so verification + hash printing find the actual files.

### Added (Phase 4 - Team Brain)

- **`team-write` role** for `VaultMount.role` joining `primary` and
  `read-only`. N team-write vaults may be mounted alongside the
  singleton primary; team-write requires `remote_url` (refused at
  config-load with `team_write_requires_remote` if absent). Canonical
  `vault_id = sha256(remote_url)[:16]` derived at validation time.
- **Per-prefix routing rules** + `auto_route` opt-in flag in
  `~/.config/engram/config.yaml`. Captures matching a `RoutingRule`
  prefix auto-route to the rule's target vault unless an explicit
  `vault:` arg or `block`/`sensitive` portability overrides per the
  pinned invariants 1+2.
- **Capture-time gate composition**
  (`engram.team.capture_gate.gate_team_capture`): the read-only-role
  refusal + member-enrollment assert + policy refuse-or-pass +
  `captured_by` stamping client-side gate.
- **Sender attribution**: `Thought.captured_by` + `Frontmatter.captured_by`
  fields. Set automatically when capturing into a `team-write` vault;
  None for personal captures and Phase 1+2+3 thoughts (backwards
  compatible). SQLite migration adds the column with NULL default.
- **GPG identity wrapper** (`engram.team.identity.GpgIdentity`):
  `gpg --list-secret-keys --with-colons` discovery + subkey-to-primary
  resolution. `assert_member_enrolled` refuses unenrolled fingerprints
  with `TeamMemberNotEnrolled`.
- **Persistent push queue**
  (`engram.team.push_queue.PersistentPushQueue`): durable per-vault
  queue at `<vault>/.engram/push-queue.local`. Survives engram restart;
  partial-line tolerance from SIGKILL mid-write; orphan-on-auth-failure
  to `<personal>/.engram/orphans/<id>.tar.gz`; disk-full at enqueue
  raises `PushQueuePersistenceFailed` (capture refusal upstream).
- **Routing dispatcher** (`engram.team.routing.resolve_target_vault`):
  5-rule precedence (block-veto, sensitive-fall-through, explicit-arg-
  wins, auto-route-match, fallback-primary) + multi-prefix tie-break
  (first prefix wins) + rule priority tie-breaker + ambiguity refusal
  with `RoutingRuleAmbiguous`.
- **`engram team-vault setup`** CLI: bootstraps a team vault by
  writing 5 canonical files (`engram.config.yaml`,
  `.engram/team-policy.yaml`, `.engram/members.yaml`, `.gitignore`,
  `.engram/setup_complete` sentinel). Idempotent + resume-after-partial.
- **`engram team-vault enroll-key`** CLI: discovers operator's GPG
  primary fingerprint and prints next-steps.
- **`engram team-vault add-member`** CLI: steward-only enrollment
  helper. Writes line-level-merge-friendly YAML.
- **`engram team-vault revoke-key`** CLI: steward-only revocation.
  Historical thoughts under the revoked key remain attributable;
  new captures refuse with `team_member_not_enrolled`.
- **Server-side `pre-receive` hook bundle**
  (`engram/team/server_hooks/pre_receive.py`): stdlib-only Python
  3.10+ script. Refuses `.indexes/` paths, `block` portability,
  `attribution_committer_mismatch`, disallowed prefixes, force-push,
  and non-steward mutation of policy/members. Lists ALL violations
  on rejection (not just the first). Hand-rolled restricted YAML
  parser (no PyYAML dep).
- **Phase 4 doctor codes** (9 new): `multiple_team_write_vaults_ok`,
  `team_member_not_enrolled`, `team_pending_pushes`,
  `team_membership_revoked`, `team_policy_violation_quarantined`,
  `serve_config_stale`, `routing_rule_priority_collision`,
  `team_vault_embedding_mismatch`, `git_branch_drifted`.
  `ALL_PHASE_4_CHECK_CODES` superset has 32 codes total (updated from 31 when `user_config_vault_name_mismatch` was added to Phase 3 in v0.5.0).
- **11 new errors**: `TeamMemberNotEnrolled`, `TeamPolicyViolation`,
  `RoutingRuleAmbiguous`, `RoutingTargetNotMounted`,
  `BlockThoughtInTeamVaultDisallowed`, `TeamVaultEmbeddingMismatch`,
  `TeamMembershipRevoked`, `AttributionCommitterMismatch`,
  `TeamWriteRequiresRemote`, `TeamVaultAlreadyInitialized`,
  `PushQueuePersistenceFailed`.
- **CaptureInputMetadata.vault** field (additive). Phase 1+2+3 clients
  omitting it see Phase 3 semantics unchanged (pinned invariant 6).
- **ADR 007 - Team Brain** documenting D1-D9 design decisions.
- **TEAM_BRAIN_GUIDE.md** operator setup walkthrough (GPG bootstrap,
  setup, hook install per platform, member add, revocation, disaster
  recovery).
- **MULTI_VAULT_SETUP.md** updated with Phase 4 role taxonomy table.

### Security (Phase 4)

- **Two-layer enforcement boundary** (pinned invariant 4): client-side
  is canonical for capture-time policies (block routing, member
  enrollment); server-side hook is canonical for push-time policies
  (allowlists, attribution integrity, force-push refusal, steward-only
  mutation). The two layers compose - a single bypass doesn't breach
  the boundary.
- **GPG-fingerprint-bound attribution**: replaces free-form
  `default_user` for team-vault sender id. Refuses captures whose
  `captured_by` doesn't match the local GPG primary fingerprint
  (client-side) and refuses pushes whose `captured_by` doesn't match
  the GPG-signed git committer fingerprint (server-side).
- **Steward-only mutation** of `team-policy.yaml` + `members.yaml`:
  enforced by the pre-receive hook reading the OLD tree's stewards
  list and refusing pushes from non-stewards.
- **`.indexes/` push refusal**: defense-in-depth; canonical
  `.gitignore` PLUS server-side hook refusal (P4-H1 mitigation).
- **Force-push refusal**: `denyNonFastForwards` PLUS server-side
  hook check (P4-H3 mitigation).

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

[Unreleased]: https://github.com/kpachhai/engram/compare/...HEAD <!-- pii-allow:repo-url -->
