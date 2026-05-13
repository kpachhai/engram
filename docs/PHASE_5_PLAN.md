# engram Phase 5 — Daemon Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `engram` v0.5.0 with daemon mode — multiple Claude Code sessions can attach engram-MCP to the same vault concurrently via a per-vault Unix domain socket daemon, with auto-spawn, idle shutdown, crash recovery, and observability.

**Architecture:** Per-vault daemon process owns `VaultLock` + `VaultStorage` + `SyncCoordinator` + `FastEmbedProvider` and listens on UDS at `<vault>/.indexes/engram.sock`. N `engram serve` proxies attach to the same daemon. Today's stdio MCP wire format preserved bit-for-bit (pinned invariant 6). Auto-spawn on first connect; idle-shutdown after 60 min default; `--no-daemon` retained as escape hatch.

**Tech Stack:** Python 3.11+, `fastmcp` (engram's pinned MCP server), `asyncio`, `fcntl.flock`, stdio JSON-RPC over UDS, `typer` CLI, Pydantic v2 strict, ruff + mypy strict, pytest + Hypothesis.

**Spec source:** `~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md` (the canonical design document — all design decisions traceable there).

**Status:** Ready to execute (post-deep-plan synthesis with 3-sub-agent critique + revision pass).

---

## Phase Numbering Note

Per the spec's Section 18, prior roadmap-only "Phase 5 Enterprise Scaffolding" + "Phase 6 Enterprise Polish" renumber to **Phase 6** and **Phase 7** respectively. This Phase 5 introduces daemon mode; old roadmap entries shift up one. Layer H4 handles the cross-repo renumber-sweep edits.

---

## Pinned-Invariant Analysis

All 7 engram pinned invariants from `CLAUDE.md` continue to hold after Phase 5 (full table in spec Section 20.5):

1. **Markdown is SoT** — unchanged. Daemon writes via the same VaultStorage facade.
2. **`portability=block` never reaches LLM** — unchanged. Daemon's resolver + portability gate + LLM tool entry points enforce independently.
3. **`portability=sensitive` only to LOCAL LLM providers** — unchanged.
4. **Two-layer enforcement at security boundaries** — unchanged.
5. **Sender identity binds to GPG primary-key fingerprint** — unchanged.
6. **MCP wire format stable for v1.x** — strictly preserved. Daemon's MCP output is byte-identical to today's `engram serve`. UDS protocol is internal, not part of the MCP wire surface.
7. **Forward-compatible markdown** — unchanged. No markdown schema changes.

The CLAUDE.md operational line "MCP server: stdio only. No HTTP. No network listener. No telemetry." gets a **clarifying amendment** in Layer H (added to ADR 008 + CLAUDE.md):

> Was: **MCP server: stdio only. No HTTP. No network listener. No telemetry.**
> Becomes: **MCP server: stdio to clients. UDS for daemon-mode internal IPC. No HTTP. No network listener (UDS is local IPC, not a network listener). No telemetry.**

---

## Phase 5 SPEC AMENDMENTS (pre-Layer-A)

Deep-plan sub-agent findings flagged 17 spec deltas. Layer A's first step (A0) amends the spec inline. The amendments preserve the spec's overall structure; they add explicit contracts where the sub-agents found ambiguity.

The amendments are listed once here as the source of truth; the plan's Layer A0 step references this section. Subsequent layers implement against the amended spec.

### Critique-pass revisions (2026-05-12)

The deep-plan critique sub-agent surfaced 5 BLOCKING + 10 SHOULD-FIX items. Addressing inline:

| Critique finding | Resolution |
|---|---|
| **B1: `atomic_write_text` has no `mode` kwarg** | Task B4 state-file write: drop `mode=0o600` from call (helper already enforces 0o600 internally). |
| **B2: VaultLock vs DaemonServer signal-handler conflict** | New Task A7 (`VaultLock` extension): add `install_signal_handlers: bool = True` kwarg. Daemon passes `False` and wires its own handler that calls `coordinator.force_flush() + storage.close() + vault_lock.release()` in order. |
| **B3: Doctor check file structure inconsistency** | Engram convention is per-feature check FUNCTIONS files (e.g., `phase3_checks.py`). For Phase 5, new file: `src/engram/diagnostics/daemon_checks.py` (no Phase N framing per CLAUDE.md). Layer E creates this file, not `doctor.py`. Updated below in Layer E. |
| **B4: `_init_engram_resources` circular dependency** | Move the `_serve_no_daemon` extraction FORWARD to a new Task A8 (refactor existing `serve.py` to factor the shared helper). Layer C then imports + calls the helper without re-implementing. Layer F just adds the proxy-mode branch + daemon subcommand registration. |
| **B5: Layers C/D/E ship with red builds** | Scope each layer's test gate to its own subdirectory: Layer A → `pytest tests/daemon/test_errors.py tests/daemon/test_config.py tests/daemon/test_socket_paths.py`; Layer B → `pytest tests/daemon/test_protocol.py tests/daemon/test_auth.py tests/daemon/test_spawn.py tests/daemon/test_state.py tests/daemon/test_log_rotation.py`; Layer C → `pytest tests/daemon/test_server.py`; Layer D → `pytest tests/daemon/test_client.py`; Layer E → `pytest tests/diagnostics/test_doctor_daemon_checks.py`; Layer F → `pytest tests/cli/ tests/daemon/`; Layer G → FULL `pytest -v` (the Phase 5 exit gate). Each layer's smoke is its own tests until Layer F wires the CLI; Layer G is the convergence point. |
| **S1: Layer A audit lacks file/line citations** | Task A6 audit doc gains explicit `src/engram/storage/facade.py` line refs + `src/engram/storage/sqlite.py` `open_connection` body excerpt. |
| **S2: FastMCP dispatch not introspected at plan time** | New Task A0.5: dispatch a sub-agent to introspect `~/.venv/.../fastmcp/` AT PLAN EXECUTION TIME (first action of Layer A). Sub-agent produces a concrete API surface map. Layer C step 1 picks Option A or B based on that map. |
| **S3: `_spawn_daemon_process` enum comparison bug** | `result.kind == "error"` → `result.is_error` (use the property). |
| **S4: `_getsockopt` socket fd ownership risk** | Switch to `socket.fromfd(fd, AF_UNIX, SOCK_STREAM)` (dups fd) → getsockopt → close the dup. Original fd untouched. |
| **S5: Layer G test bodies are stubs** | Layer G is explicitly a **test contract list** — each test entry specifies the scenario + expected assertion shape. Layer G's commit step DOES NOT ship with `...` placeholders; the layer execution writes the test body for each contract before committing. New Layer G preamble + per-test "Contract / Implementation hint" structure replaces stub bodies. |
| **S6: Smoke test assertion contradicts socket lifecycle** | `test_smoke_engram_serve_proxy_default_cold`: change to `assert (vault / ".indexes" / "engram.sock").exists() is True` (socket persists until daemon idle-shutdown); add `engram daemon stop` to teardown. |
| **S7: Baseline test count uncited** | New Task A0.7: snapshot `uv run pytest --collect-only -q | tail -1` to `docs/PHASE_5_BASELINE.md` so regression deltas are anchored. |
| **S8: fastmcp pin not audited** | Same Task A0.5 audit also records `uv.lock` fastmcp version. |
| **S9: Log rotation 0o644 window** | `EngramRotatingHandler.__init__`: explicit `os.umask(0o077)` before super().__init__ + restore umask after. |
| **S10: Naming inconsistency** | Settle: one helper named `_init_serve_runtime` returns `(VaultLock, VaultStorage, SyncCoordinator, FastEmbedProvider, FastMCPServer)`. Called by both daemon's `serve_forever` AND `_serve_no_daemon`. Layer A8 extracts; Layer C imports. |

### Amendment 1 — Section 5.6: Daemon startup ordering contract

**Add to spec Section 5.6:**

The daemon spawn dance (executed inside the forked child) MUST follow this exact order:

1. Install daemon-specific signal handlers for SIGTERM + SIGINT (replacing any inherited parent handlers).
2. Acquire `VaultLock` via `fcntl.flock`. On `LockError`: write `error: <msg>\n` to the readiness pipe, exit nonzero.
3. Run startup probes (existing path).
4. Detect cloud-sync vault path (existing path).
5. Open `VaultStorage`, build `SyncCoordinator`, construct `FastEmbedProvider`.
6. Build the FastMCP server (single-vault or multivault per `_build_multivault_server_for`).
7. `unlink(<vault>/.indexes/engram.sock, missing_ok=True)` — clean any prior inode at the socket path.
8. `bind()` the UDS at the socket path.
9. `chmod(0o600)` the socket file as belt-and-suspenders.
10. Write `engram.state.json` with PID, started_at, vault_name, **hostname**, config snapshot.
11. Write `ready\n` to the readiness pipe; close pipe.
12. Enter accept loop.

Ordering rationale (closes risks H1, M5, L2):
- Signal handlers BEFORE resource acquisition → SIGTERM during init still cleans up.
- `VaultLock` BEFORE `bind` → two racing spawners cannot both pass past step 2; one fails fast at the lock.
- `unlink` BEFORE `bind` → stale-socket recovery is part of the spawn dance.
- `state.json` AFTER bind → `engram daemon status` can trust the file when it exists.
- `ready\n` LAST → proxy only attaches once daemon is fully ready.

### Amendment 2 — Section 5.1: Coordinator-drain contract

**Add to spec Section 5.1 (`daemon-shutdown-initiated` state):**

Drain proceeds in this order with explicit time bounds:

1. Stop accepting new UDS connections (close listener immediately).
2. Wait for all in-flight per-connection tasks to complete OR force-cancel after `daemon.shutdown_drain_seconds` (default 5s; new config field).
3. Call `coordinator.force_flush()` with timeout `daemon.coordinator_flush_seconds` (default 30s; new config field) — DISTINCT from the outer `engram daemon stop` 10s wait. Long git pushes get their own budget.
4. Close `VaultStorage`.
5. Release `VaultLock`.
6. Unlink `engram.sock`.
7. Unlink `engram.state.json`.
8. Flush + close log handlers.
9. `os._exit(0)`.

`engram daemon stop` (CLI) outer timeout default raises from 10s to 60s to give the coordinator a chance to drain large push backlogs cleanly. `--force` still SIGKILLs at 5s if explicitly requested.

This closes risk H2.

### Amendment 3 — Section 5.4: Two-phase atomic idle shutdown

**Replace spec Section 5.4 idle-shutdown logic:**

Idle shutdown uses two-phase atomicity:

1. Idle timer fires.
2. Daemon acquires an internal `asyncio.Lock` (`_shutdown_lock`).
3. Re-check connected_proxies under lock: if > 0 (a new proxy connected between fire and lock acquire), CANCEL shutdown (reset timer when next proxy disconnects); else proceed.
4. Atomically close the UDS listener (no new accept() can succeed past this point).
5. Release `_shutdown_lock`.
6. Proceed with the coordinator-drain contract (Amendment 2).

A new metric `connect_during_drain` (counter) is incremented if any `accept()` raises after step 4 — surfaced in `engram daemon status`. This closes risk H3.

### Amendment 4 — Section 11.1: WAL recovery grace in spawn timeout

**Add to spec Section 11.1 (`DaemonConfig`):**

```python
class DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_spawn: bool = True
    idle_shutdown_seconds: int = Field(default=3600, ge=0)
    spawn_timeout_seconds: int = Field(default=30, ge=1)
    spawn_lock_timeout_seconds: int = Field(default=10, ge=1)
    wal_recovery_grace_seconds: int = Field(default=60, ge=0)  # NEW (closes H4)
    shutdown_drain_seconds: int = Field(default=5, ge=1)        # NEW (closes H2)
    coordinator_flush_seconds: int = Field(default=30, ge=1)    # NEW (closes H2)
    connection_idle_timeout_seconds: int = Field(default=86400, ge=0)  # 24h, NEW (closes L3+B1)
    max_frame_bytes: int = Field(default=16 * 1024 * 1024, ge=64 * 1024)  # 16 MB, NEW (closes G5)
    log_max_size_mb: int = Field(default=100, ge=1)
    log_retention_days: int = Field(default=7, ge=1)
    log_level: str = Field(default="INFO")
    log_redact_thought_content: bool = Field(default=True)      # NEW (closes M2)
```

Effective spawn-timeout when daemon detects `engram.db-wal > 10 MB`: `spawn_timeout_seconds + wal_recovery_grace_seconds`. This closes H4 + adds Pydantic bounds on every numeric field (closes E2-E5).

### Amendment 5 — Section 12.1 (`socket_paths.py`): UDS path-length check

**Add to spec Section 12.1:**

`socket_paths.py` enforces:
- Resolve `<vault>/.indexes/engram.sock` via `Path.resolve(strict=False)`.
- If `len(str(resolved)) >= 104`: raise `DaemonError("UDS path too long for macOS (max 104 bytes): <path>. Workaround: symlink your vault dir into ~/.engram-vaults/<short-name>/")`.
- Same check for `engram.spawn.lock`, `engram.state.json`, `engram.log`.

Layer E adds doctor check `daemon_socket_path_too_long`. This closes H5 + D5.

### Amendment 6 — Section 6.2: Max frame size

**Add to spec Section 6.2:**

The daemon's per-connection `asyncio.StreamReader` is constructed with `limit=DaemonConfig.max_frame_bytes` (default 16 MB). Proxy uses the same limit. On `LimitOverrunError`: close connection cleanly with JSON-RPC error `-32600` ("Invalid request: frame exceeds max_frame_bytes"). Closes G5.

### Amendment 7 — Section 10.3-10.4: `daemon status` not-running output

**Add to spec Section 10.3-10.4:**

When no daemon is running for the target vault, `engram daemon status` exits 0 (not 1 — not-running is normal state) and prints:

```
vault     : memex (~/.local/share/engram/memex)
daemon    : not running
socket    : not present at <path>
state file: not present at <path>
hint      : run `engram serve` (auto-spawn) or `engram daemon start --vault memex`
```

JSON form:

```json
{
  "vault": {"name": "memex", "path": "..."},
  "daemon": {"running": false, "pid": null, "started_at": null, "uptime_seconds": null, "rss_bytes": null},
  "socket": {"present": false, "path": "..."},
  "state_file": {"present": false, "path": "..."},
  "activity": null,
  "coordinator": null,
  "log": {"path": "...", "size_bytes": null, "present": false}
}
```

Consumers branch on `daemon.running`. This closes F1+F2.

### Amendment 8 — Section 13.3: Log rotation + follow contract

**Add to spec Section 13.3:**

- `daemon.log_max_size_mb=0`: NOT permitted (Pydantic `ge=1`). To disable rotation: set very large value (e.g., 1_048_576 = 1 TB).
- `engram daemon logs --follow` uses `WatchedFileHandler`-style inode-reopen logic — on log rotation, follower detects rename + reopens. No silent drop. Implementation: tail-poll loop checks inode every 100 ms via `os.stat`.
- `daemon.log_level=DEBUG` adds a warning banner to `engram daemon logs` output: `[engram-daemon DEBUG mode active — log may contain thought content; treat as PII]`.

Closes F4 + M2.

### Amendment 9 — Section 13: Log content redaction

**Add to spec Section 13:**

Default (`log_redact_thought_content=true`): the daemon's per-request log line emits ONLY fingerprint + bytes-of-content, NOT thought text. Format: `request=capture_thought fingerprint=<hex> bytes=<int> proxy_pid=<int>`. Tool-call ERROR paths similarly redact request content (log fingerprint + error class, not content).

When `log_redact_thought_content=false`: full content logged. The DEBUG-mode banner above also fires.

Closes M2.

### Amendment 10 — Section 22: Risk #M6 downgrade procedure

**Add to spec Section 22 + Layer H release-notes (CHANGELOG):**

v0.5.0 release notes explicitly document:

> **Downgrade gotcha:** if a v0.5.0 daemon is running, stop it (`engram daemon stop`) before installing v0.4.x. Otherwise v0.4.x's `engram serve` will fail with `LockError` until you `kill` the v0.5.0 daemon manually (v0.4.x doesn't know about daemon mode). Removing the `daemon:` block from your `engram.config.yaml` is also required since v0.4.x's Pydantic model is `extra="forbid"`.

Closes M6.

### Amendment 11 — Section 14: Test additions

**Add to spec Section 14:**

- **N=100 spawn race property test** in `tests/properties/test_daemon_spawn_race.py`: `asyncio.gather(*[spawn_subprocess()] * 100)`; assert exactly one daemon PID exists; all 100 proxies attach OR receive a clean `DaemonSpawnError` with retry hint.
- **FastMCP per-connection dispatch isolation test** in `tests/daemon/test_dispatch_isolation.py`: 2 proxies connect; each sends an MCP request with distinct `id`; assert response routes only to its origin connection. Guards against M4's "future fastmcp bump causes response cross-talk."
- **Embedding cache concurrent write test** in `tests/daemon/test_embedding_cache_concurrency.py`: 50 concurrent embeds with identical text; assert cache file inode is stable + content is uncorrupted. Closes M1.

---

## Code-Side vs Operational Exit Criteria Split

Per engram CLAUDE.md "Code Project Completion Gate":

**Code-side criteria** (Phase 5 ships when all met — verifiable from repo state alone):
- All 8 layers' commits landed on main.
- 1166 baseline tests still pass (no regressions); ~80-100 new tests pass; coverage ≥ 80%.
- `uv run ruff format && uv run ruff check && uv run mypy` all clean.
- 9 new hermetic CLI smoke tests pass against the installed binary.
- Spec audit (`verify-before-done` Section 6) — no MISSING items.
- Comprehension gate Step 5 — 4-question artifact authored by maintainer.

**Operational criterion** (deferred to live deployment):
- **Phase 5 Op #1:** Maintainer runs 2 concurrent Claude Code sessions against memex personal vault for ≥7 consecutive days. `engram daemon status` checked daily for proxy count + uptime + error counter. Laptop sleep/wake cycles survived without orphaned sockets. No fallback to `--no-daemon` or per-session vaults.

Added to `~/repos/github.com/kpachhai/idea-forge/workspace/engram/PENDING_TASKS.md` in Layer H.

---

## Layer A — Errors + Config + Path helpers + Doctor scaffolding

**Goal:** Establish the foundational types, configuration surface, path helpers, and doctor scaffolding that Layers B-H depend on. Reconcile the spec with deep-plan sub-agent findings.

**Files this layer creates or modifies:**
- Modify: `~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md` (Amendments 1-11 above)
- Modify: `src/engram/errors.py` (add 5 new error classes)
- Modify: `src/engram/config/models.py` (add `DaemonConfig`; wire into `EffectiveConfig`)
- Modify: `src/engram/config/loader.py` (5-layer precedence for `DaemonConfig`)
- Create: `src/engram/daemon/__init__.py` (subpackage init)
- Create: `src/engram/daemon/socket_paths.py` (path resolver + length check)
- Modify: `src/engram/diagnostics/check_codes.py` (register 5 daemon check code constants)
- Create: `tests/daemon/__init__.py`
- Create: `tests/daemon/test_errors.py`
- Create: `tests/daemon/test_config.py`
- Create: `tests/daemon/test_socket_paths.py`

### Layer A revised task order (post-critique)

Execute Layer A tasks in this order — critique-pass additions are A0.5, A0.7, A7, A8:

| Order | Task | Headline |
|---|---|---|
| 1 | A0 | Apply 11 spec amendments |
| 2 | A0.5 | FastMCP API introspection sub-agent (resolves spec Audit 2 + critique S2 + S8) |
| 3 | A0.7 | Baseline test count snapshot (`PHASE_5_BASELINE.md`; closes critique S7) |
| 4 | A1 | Daemon error family |
| 5 | A2 | DaemonConfig Pydantic model |
| 6 | A3 | Config loader wiring |
| 7 | A4 | socket_paths.py |
| 8 | A5 | Daemon doctor check code constants |
| 9 | A6 | Pre-implementation audit doc (SQLite + FastMCP) — now includes A0.5 sub-agent findings |
| 10 | **A7 (NEW)** | VaultLock `install_signal_handlers` kwarg (closes critique B2) |
| 11 | **A8 (NEW)** | Extract `_init_serve_runtime` from `cli/serve.py` (closes critique B4 + S10) |
| 12 | Layer A commit | Single commit landing A0-A8 |

The new tasks are described inline below in numeric order.

### Task A0: Apply spec amendments

**Files:**
- Modify: `~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md`

- [ ] **Step 1: Apply the 11 amendments from the "Phase 5 SPEC AMENDMENTS" section above**

Each amendment is a delta against the spec's existing section. Apply via `Edit` tool with `old_string` matching the existing section text and `new_string` containing the amendment-added content. Amendments touch: Section 5.1, 5.4, 5.6, 6.2, 10.3-10.4, 11.1, 12.1, 13, 13.3, 14, 22.

- [ ] **Step 2: Verify amendments applied**

Run: `grep -n "Amendment\\|wal_recovery_grace_seconds\\|connection_idle_timeout_seconds\\|max_frame_bytes\\|shutdown_drain_seconds\\|coordinator_flush_seconds\\|log_redact_thought_content\\|connect_during_drain" ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md`
Expected: 8+ matches across the amended sections.

### Task A0.5: FastMCP API introspection sub-agent (NEW — critique S2 + S8)

**Files:**
- Inspect: `~/repos/github.com/kpachhai/engram/.venv/lib/python*/site-packages/fastmcp/`
- Inspect: `~/repos/github.com/kpachhai/engram/uv.lock` (fastmcp version pin)
- Create: `docs/PHASE_5_FASTMCP_AUDIT.md`

- [ ] **Step 1: Dispatch a research sub-agent**

```
Read the installed fastmcp version pinned at uv.lock. Then read its source.
Document specifically:
1. Is there a public method on FastMCP (or any FastMCP transport) that takes
   ONE JSON-RPC frame and returns the response (without owning the stdio loop)?
2. If yes, what's the signature?
3. If no, identify the lowest-level entrypoint we'd need to call to dispatch a
   single tool call against the registered tools.
4. What version is fastmcp pinned to in uv.lock?
5. Are there any breaking changes between this version and the latest fastmcp
   release that would affect a per-connection dispatch implementation?

Return findings as a markdown document.
```

- [ ] **Step 2: Document findings in `docs/PHASE_5_FASTMCP_AUDIT.md`**

The document is the input to Layer C step 1's Option A vs B decision. Include:
- Pinned fastmcp version (from `uv.lock`).
- The chosen dispatch entrypoint (function/method name + signature).
- A confidence level (HIGH if we found a documented public API; MEDIUM if we're using an internal function).
- Mitigation if MEDIUM: add the dispatch-isolation test (Layer G G7) as the canary against future fastmcp changes.

### Task A0.7: Baseline test count snapshot (NEW — critique S7)

**Files:**
- Create: `docs/PHASE_5_BASELINE.md`

- [ ] **Step 1: Capture baseline test metrics**

```bash
cd ~/repos/github.com/kpachhai/engram
uv run pytest --collect-only -q 2>&1 | tail -3 > /tmp/baseline.txt
uv run pytest tests/test_phase4_cli_smoke.py --collect-only -q 2>&1 | tail -3 >> /tmp/baseline.txt
uv run pytest --cov=src --cov-fail-under=0 2>&1 | grep "TOTAL" >> /tmp/baseline.txt
cat /tmp/baseline.txt
```

- [ ] **Step 2: Write `docs/PHASE_5_BASELINE.md`**

```markdown
# Phase 5 Baseline Metrics (Pre-Implementation)

Captured: 2026-05-XX (Layer A task A0.7)

## Test counts

- Total collected: <N> tests (from `pytest --collect-only`)
- Smoke tests in test_phase4_cli_smoke.py: <M>
- Coverage: <X>% (from `pytest --cov`)

## Regression deltas at Phase 5 close

Phase 5 acceptance: total tests >= <N> + 80 (Phase 5 adds ~80 new tests).
Smoke tests >= <M> + 9 (Phase 5 adds 9 new smoke tests).
Coverage remains >= 80% (the `--cov-fail-under=80` gate).
```

### Task A1: Daemon error family

**Files:**
- Modify: `src/engram/errors.py`
- Create: `tests/daemon/test_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_errors.py
"""Test the daemon error family inherits EngramError and has unique error codes."""
import pytest

from engram.errors import (
    DaemonConnectionError,
    DaemonError,
    DaemonNotRunningError,
    DaemonSpawnError,
    EngramError,
    PeerCredRejectError,
)


def test_all_daemon_errors_inherit_engram_error():
    for cls in (DaemonError, DaemonSpawnError, DaemonConnectionError, DaemonNotRunningError, PeerCredRejectError):
        assert issubclass(cls, EngramError)


def test_error_codes_unique_and_named():
    codes = {
        DaemonError.error_code,
        DaemonSpawnError.error_code,
        DaemonConnectionError.error_code,
        DaemonNotRunningError.error_code,
        PeerCredRejectError.error_code,
    }
    assert len(codes) == 5
    assert codes == {
        "daemon_error",
        "daemon_spawn_error",
        "daemon_connection_error",
        "daemon_not_running_error",
        "peer_cred_reject_error",
    }


def test_subtypes_inherit_daemon_error():
    for cls in (DaemonSpawnError, DaemonConnectionError, DaemonNotRunningError, PeerCredRejectError):
        assert issubclass(cls, DaemonError)


def test_message_preserved():
    err = DaemonSpawnError("ready signal timed out")
    assert "ready signal timed out" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_errors.py -v`
Expected: FAIL with `ImportError` (classes don't exist yet).

- [ ] **Step 3: Implement DaemonError + 4 subclasses in `src/engram/errors.py`**

Append to existing `errors.py`:

```python
class DaemonError(EngramError):
    """Base class for all daemon-mode errors."""

    error_code = "daemon_error"


class DaemonSpawnError(DaemonError):
    """Daemon spawn dance failed (timeout, lock contention, init failure)."""

    error_code = "daemon_spawn_error"


class DaemonConnectionError(DaemonError):
    """Proxy could not connect to the daemon over UDS."""

    error_code = "daemon_connection_error"


class DaemonNotRunningError(DaemonError):
    """No daemon is running and auto-spawn is disabled."""

    error_code = "daemon_not_running_error"


class PeerCredRejectError(DaemonError):
    """Peer credential check rejected a connection from a non-self UID."""

    error_code = "peer_cred_reject_error"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/daemon/test_errors.py -v`
Expected: PASS (4 tests).

### Task A2: DaemonConfig Pydantic model

**Files:**
- Modify: `src/engram/config/models.py`
- Create: `tests/daemon/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_config.py
"""Test DaemonConfig Pydantic model with Field bounds + defaults."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from engram.config.models import DaemonConfig


def test_defaults_match_spec():
    cfg = DaemonConfig()
    assert cfg.auto_spawn is True
    assert cfg.idle_shutdown_seconds == 3600
    assert cfg.spawn_timeout_seconds == 30
    assert cfg.spawn_lock_timeout_seconds == 10
    assert cfg.wal_recovery_grace_seconds == 60
    assert cfg.shutdown_drain_seconds == 5
    assert cfg.coordinator_flush_seconds == 30
    assert cfg.connection_idle_timeout_seconds == 86400
    assert cfg.max_frame_bytes == 16 * 1024 * 1024
    assert cfg.log_max_size_mb == 100
    assert cfg.log_retention_days == 7
    assert cfg.log_level == "INFO"
    assert cfg.log_redact_thought_content is True


def test_extra_forbid():
    with pytest.raises(ValidationError) as exc_info:
        DaemonConfig.model_validate({"unknown_field": True})
    assert "Extra inputs are not permitted" in str(exc_info.value)


@pytest.mark.parametrize("field,bad_value,reason", [
    ("idle_shutdown_seconds", -1, "ge=0"),
    ("spawn_timeout_seconds", 0, "ge=1"),
    ("spawn_lock_timeout_seconds", 0, "ge=1"),
    ("wal_recovery_grace_seconds", -1, "ge=0"),
    ("shutdown_drain_seconds", 0, "ge=1"),
    ("coordinator_flush_seconds", 0, "ge=1"),
    ("connection_idle_timeout_seconds", -1, "ge=0"),
    ("max_frame_bytes", 32_000, "ge=65536"),
    ("log_max_size_mb", 0, "ge=1"),
    ("log_retention_days", 0, "ge=1"),
])
def test_field_lower_bounds_enforced(field, bad_value, reason):
    with pytest.raises(ValidationError) as exc_info:
        DaemonConfig.model_validate({field: bad_value})
    assert field in str(exc_info.value)


def test_idle_shutdown_zero_means_never():
    cfg = DaemonConfig(idle_shutdown_seconds=0)
    assert cfg.idle_shutdown_seconds == 0  # 0 is allowed; means "never auto-shutdown"


def test_huge_idle_shutdown_accepted():
    cfg = DaemonConfig(idle_shutdown_seconds=999_999_999)
    assert cfg.idle_shutdown_seconds == 999_999_999
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_config.py -v`
Expected: FAIL — `DaemonConfig` not importable.

- [ ] **Step 3: Implement DaemonConfig in `src/engram/config/models.py`**

Add to `src/engram/config/models.py` (after existing config models):

```python
from pydantic import Field


class DaemonConfig(BaseModel):
    """Phase 5 daemon-mode configuration. Spec: 2026-05-12-engram-daemon-mode-design.md Section 11."""

    model_config = ConfigDict(extra="forbid")

    auto_spawn: bool = True
    idle_shutdown_seconds: int = Field(default=3600, ge=0)
    spawn_timeout_seconds: int = Field(default=30, ge=1)
    spawn_lock_timeout_seconds: int = Field(default=10, ge=1)
    wal_recovery_grace_seconds: int = Field(default=60, ge=0)
    shutdown_drain_seconds: int = Field(default=5, ge=1)
    coordinator_flush_seconds: int = Field(default=30, ge=1)
    connection_idle_timeout_seconds: int = Field(default=86400, ge=0)
    max_frame_bytes: int = Field(default=16 * 1024 * 1024, ge=65536)
    log_max_size_mb: int = Field(default=100, ge=1)
    log_retention_days: int = Field(default=7, ge=1)
    log_level: str = Field(default="INFO")
    log_redact_thought_content: bool = Field(default=True)
```

Then wire into `EffectiveConfig`:

```python
class EffectiveConfig(BaseModel):
    # ... existing fields ...
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/daemon/test_config.py -v`
Expected: PASS (all parametrized cases + 4 standalone).

### Task A3: Config loader 5-layer precedence for DaemonConfig

**Files:**
- Modify: `src/engram/config/loader.py`
- Modify: `tests/daemon/test_config.py` (add loader-level tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/daemon/test_config.py`:

```python
from pathlib import Path

from engram.config.loader import load_config


def test_daemon_config_loaded_from_per_vault_yaml(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    (vault_dir / ".engram").mkdir(parents=True)
    config_path = vault_dir / ".engram" / "config.yaml"
    config_path.write_text(
        "vault_name: test\n"
        "daemon:\n"
        "  idle_shutdown_seconds: 7200\n"
        "  log_max_size_mb: 50\n"
    )
    cfg = load_config(explicit_vault_config=config_path)
    assert cfg.daemon.idle_shutdown_seconds == 7200
    assert cfg.daemon.log_max_size_mb == 50
    # defaults preserved for unspecified fields
    assert cfg.daemon.spawn_timeout_seconds == 30


def test_daemon_config_empty_block_uses_defaults(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    (vault_dir / ".engram").mkdir(parents=True)
    config_path = vault_dir / ".engram" / "config.yaml"
    config_path.write_text("vault_name: test\ndaemon: {}\n")
    cfg = load_config(explicit_vault_config=config_path)
    assert cfg.daemon.idle_shutdown_seconds == 3600


def test_daemon_config_missing_block_uses_defaults(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    (vault_dir / ".engram").mkdir(parents=True)
    config_path = vault_dir / ".engram" / "config.yaml"
    config_path.write_text("vault_name: test\n")
    cfg = load_config(explicit_vault_config=config_path)
    assert cfg.daemon.idle_shutdown_seconds == 3600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_config.py::test_daemon_config_loaded_from_per_vault_yaml -v`
Expected: FAIL — loader doesn't know about `daemon` key.

- [ ] **Step 3: Wire DaemonConfig into the loader's merge logic in `src/engram/config/loader.py`**

The existing loader composes layered Pydantic models. Add `daemon` to the keys merged across layers. Verify via the loader's existing layer-precedence behavior — the merge is dictionary-shaped before Pydantic validation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/daemon/test_config.py -v`
Expected: PASS (full file, including new loader tests).

### Task A4: socket_paths.py — path resolver + UDS length check

**Files:**
- Create: `src/engram/daemon/__init__.py`
- Create: `src/engram/daemon/socket_paths.py`
- Create: `tests/daemon/test_socket_paths.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_socket_paths.py
"""Path resolver + UDS length-limit enforcement."""
from __future__ import annotations

from pathlib import Path

import pytest

from engram.daemon.socket_paths import (
    SocketPaths,
    UDS_PATH_LIMIT_BYTES,
    resolve_paths,
)
from engram.errors import DaemonError


def test_resolve_paths_returns_co_located(tmp_path: Path):
    vault = tmp_path / "memex"
    (vault / ".indexes").mkdir(parents=True)
    paths = resolve_paths(vault)
    assert isinstance(paths, SocketPaths)
    assert paths.socket == (vault / ".indexes" / "engram.sock").resolve()
    assert paths.spawn_lock == (vault / ".indexes" / "engram.spawn.lock").resolve()
    assert paths.state_file == (vault / ".indexes" / "engram.state.json").resolve()
    assert paths.log_file == (vault / ".indexes" / "engram.log").resolve()


def test_resolve_paths_creates_indexes_dir_if_missing(tmp_path: Path):
    vault = tmp_path / "memex"
    vault.mkdir()
    # No .indexes/ yet
    paths = resolve_paths(vault)
    assert (vault / ".indexes").exists()
    assert paths.socket.parent == (vault / ".indexes").resolve()


def test_resolve_paths_rejects_long_uds_path(tmp_path: Path):
    long_name = "x" * 120
    vault = tmp_path / long_name / "memex"
    vault.mkdir(parents=True)
    with pytest.raises(DaemonError) as exc_info:
        resolve_paths(vault)
    assert "UDS path too long" in str(exc_info.value)
    assert str(UDS_PATH_LIMIT_BYTES) in str(exc_info.value)


def test_uds_path_limit_byte_constant():
    # macOS has 104; Linux has 108; we use the stricter to be portable
    assert UDS_PATH_LIMIT_BYTES == 104


def test_resolve_paths_resolves_symlinks(tmp_path: Path):
    real_vault = tmp_path / "real"
    real_vault.mkdir()
    link_vault = tmp_path / "link"
    link_vault.symlink_to(real_vault)
    paths = resolve_paths(link_vault)
    # resolved path should be the real one
    assert paths.socket.parent == (real_vault / ".indexes").resolve()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_socket_paths.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/engram/daemon/__init__.py`**

```python
"""engram daemon mode (Phase 5). Spec: 2026-05-12-engram-daemon-mode-design.md."""

from engram.daemon.socket_paths import SocketPaths, resolve_paths

__all__ = ["SocketPaths", "resolve_paths"]
```

- [ ] **Step 4: Implement `src/engram/daemon/socket_paths.py`**

```python
"""Per-vault path resolution for daemon-mode files.

Co-located with the existing engram.lock under <vault>/.indexes/:

- engram.sock          UDS daemon listens on
- engram.spawn.lock    flock for spawn-race serialization (brief)
- engram.state.json    PID, started_at, vault_name, hostname, config snapshot
- engram.log           daemon stdout/stderr (rotated)

Enforces the macOS UDS path-length limit (104 bytes) at resolve time so
callers fail fast rather than at bind() with a confusing OSError.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from engram.errors import DaemonError

_INDEXES_SUBDIR = ".indexes"
_SOCKET_FILENAME = "engram.sock"
_SPAWN_LOCK_FILENAME = "engram.spawn.lock"
_STATE_FILENAME = "engram.state.json"
_LOG_FILENAME = "engram.log"

# macOS limit is 104 (sun_path is 104 bytes incl. null); Linux is 108.
# Use 104 for portability.
UDS_PATH_LIMIT_BYTES: Final[int] = 104


@dataclass(frozen=True)
class SocketPaths:
    """Resolved per-vault daemon-mode paths."""

    vault: Path
    indexes_dir: Path
    socket: Path
    spawn_lock: Path
    state_file: Path
    log_file: Path


def resolve_paths(vault: Path) -> SocketPaths:
    """Resolve and validate daemon-mode paths for a vault.

    Creates `<vault>/.indexes/` if missing. Raises DaemonError if the
    socket path exceeds UDS_PATH_LIMIT_BYTES.
    """
    vault = Path(vault).expanduser().resolve()
    indexes_dir = vault / _INDEXES_SUBDIR
    indexes_dir.mkdir(parents=True, exist_ok=True)
    socket = (indexes_dir / _SOCKET_FILENAME).resolve()
    spawn_lock = (indexes_dir / _SPAWN_LOCK_FILENAME).resolve()
    state_file = (indexes_dir / _STATE_FILENAME).resolve()
    log_file = (indexes_dir / _LOG_FILENAME).resolve()

    socket_bytes = str(socket).encode("utf-8")
    if len(socket_bytes) >= UDS_PATH_LIMIT_BYTES:
        msg = (
            f"UDS path too long for macOS (max {UDS_PATH_LIMIT_BYTES} bytes): "
            f"{socket} ({len(socket_bytes)} bytes). "
            f"Workaround: symlink your vault dir into ~/.engram-vaults/<short-name>/"
        )
        raise DaemonError(msg)

    return SocketPaths(
        vault=vault,
        indexes_dir=indexes_dir,
        socket=socket,
        spawn_lock=spawn_lock,
        state_file=state_file,
        log_file=log_file,
    )


__all__ = ["SocketPaths", "UDS_PATH_LIMIT_BYTES", "resolve_paths"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/daemon/test_socket_paths.py -v`
Expected: PASS (5 tests).

### Task A5: Register 5 daemon doctor check codes (stubs)

**Files:**
- Modify: `src/engram/diagnostics/check_codes.py`

- [ ] **Step 1: Add 5 new check code constants + group tuple**

Append to `src/engram/diagnostics/check_codes.py`:

```python
# Daemon-mode check codes (Phase 5; spec 2026-05-12-engram-daemon-mode-design.md Section 13.2).
DAEMON_RUNNING: Final[str] = "daemon_running"
DAEMON_SOCKET_PERMISSIONS: Final[str] = "daemon_socket_permissions"
DAEMON_SOCKET_STALE: Final[str] = "daemon_socket_stale"
DAEMON_LOG_ROTATION_HEALTHY: Final[str] = "daemon_log_rotation_healthy"
DAEMON_UPTIME_EXCESSIVE: Final[str] = "daemon_uptime_excessive"
DAEMON_SOCKET_PATH_TOO_LONG: Final[str] = "daemon_socket_path_too_long"

ALL_DAEMON_CHECK_CODES: Final[tuple[str, ...]] = (
    DAEMON_RUNNING,
    DAEMON_SOCKET_PERMISSIONS,
    DAEMON_SOCKET_STALE,
    DAEMON_LOG_ROTATION_HEALTHY,
    DAEMON_UPTIME_EXCESSIVE,
    DAEMON_SOCKET_PATH_TOO_LONG,
)
```

- [ ] **Step 2: Verify constants exist**

Run: `uv run python -c "from engram.diagnostics.check_codes import ALL_DAEMON_CHECK_CODES; print(len(ALL_DAEMON_CHECK_CODES))"`
Expected: `6` (5 from Section 13.2 + the path-too-long from Amendment 5).

### Task A6: Layer A audit confirmation

**Files:**
- Create: `docs/PHASE_5_LAYER_A_AUDIT.md` (short note documenting the two open Layer A items the code-analysis sub-agent resolved)

- [ ] **Step 1: Document the two audits**

Create `docs/PHASE_5_LAYER_A_AUDIT.md`:

```markdown
# Phase 5 Layer A — Pre-implementation audits

Spec source: ../../idea-forge/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md

## Audit 1: VaultStorage SQLite connection model

**Question:** Does the existing `VaultStorage` use one shared SQLite connection across handlers, or per-call connections? This determines whether daemon mode needs an `asyncio.Lock` around writes.

**Answer (from deep-plan code-analysis sub-agent):** ONE shared connection per `VaultStorage` instance (opened via `engram.storage.sqlite.open_connection()` with WAL mode + sqlite-vec). Per-call ACCESS through this single connection. Concurrent reads safe via SQLite's WAL snapshot semantics; writes serialized via the single process holding `VaultLock`.

**Implication for daemon mode:** the daemon holds the only `VaultLock` and owns the only `VaultStorage` for that vault. Concurrent per-connection asyncio tasks all dispatch through this single storage. SQLite WAL's at-most-one-writer guarantee, combined with the single-storage / single-connection model, means writes naturally serialize at the SQLite engine level. **No additional `asyncio.Lock` is required around the write path** for Phase 5 — Phase 4's storage facade is daemon-mode-safe as-is.

If property test G2 (concurrent captures from N proxies) ever fails due to race symptoms, revisit this audit.

## Audit 2: FastMCP dispatch entrypoint

**Question:** Does FastMCP expose a per-request dispatch entrypoint, or only `server.run()` (the all-or-nothing stdio loop)?

**Answer (from deep-plan code-analysis sub-agent):** Only `server.run()` is exposed. FastMCP runs a blocking stdio loop. There is no per-connection dispatch entrypoint.

**Implication for daemon mode:** Layer C (`daemon/server.py`) cannot reuse `server.run()` for the UDS-based daemon. Instead, Layer C constructs the FastMCP server and either:

- **Option A (preferred):** Uses FastMCP's internal request-handler primitives if accessible (introspect `fastmcp` to find them); wraps each accepted UDS connection in a custom per-connection dispatch loop that reads JSON-RPC frames + dispatches against the same FastMCP instance.
- **Option B (fallback):** Reuses FastMCP's tool registry but builds its own JSON-RPC parse/dispatch/serialize loop in `daemon/server.py`. ~80 LOC of additional code; preserves invariant 6 because the wire format is JSON-RPC regardless of who's parsing.

Layer C step 1 selects between A and B based on what FastMCP actually exposes. Decision documented inline in `daemon/server.py`'s module docstring.
```

- [ ] **Step 2: Verify audit file exists + readable**

Run: `wc -l docs/PHASE_5_LAYER_A_AUDIT.md`
Expected: ~35 lines.

### Task A7: VaultLock `install_signal_handlers` kwarg (NEW — critique B2)

**Files:**
- Modify: `src/engram/utils/lock.py`
- Create: `tests/utils/test_vault_lock_signal_handlers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/utils/test_vault_lock_signal_handlers.py
"""VaultLock with install_signal_handlers=False — daemon owns signal handling."""
from __future__ import annotations

import signal
from pathlib import Path

import pytest

from engram.utils.lock import VaultLock


def test_install_signal_handlers_default_true(tmp_path: Path):
    """Default behavior unchanged: VaultLock installs its own SIGTERM/SIGINT handlers."""
    vault = tmp_path / "vault"
    vault.mkdir()
    original_sigterm = signal.getsignal(signal.SIGTERM)
    with VaultLock(vault):
        # Handler should be overridden
        assert signal.getsignal(signal.SIGTERM) is not original_sigterm
    # On release, handler restored
    assert signal.getsignal(signal.SIGTERM) is original_sigterm


def test_install_signal_handlers_false_leaves_handlers_alone(tmp_path: Path):
    """Daemon use case: VaultLock does NOT touch signal handlers."""
    vault = tmp_path / "vault"
    vault.mkdir()
    original_sigterm = signal.getsignal(signal.SIGTERM)
    with VaultLock(vault, install_signal_handlers=False):
        # Handler unchanged
        assert signal.getsignal(signal.SIGTERM) is original_sigterm
    assert signal.getsignal(signal.SIGTERM) is original_sigterm
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/utils/test_vault_lock_signal_handlers.py -v`
Expected: FAIL — kwarg doesn't exist.

- [ ] **Step 3: Extend VaultLock**

In `src/engram/utils/lock.py`:
- Add `install_signal_handlers: bool = True` parameter to `__init__`.
- Skip `_install_cleanup_hooks` when `install_signal_handlers=False`.
- The daemon (Layer C) passes `install_signal_handlers=False` AND wires its own SIGTERM/SIGINT handler that calls `coordinator.force_flush() + storage.close() + vault_lock.release()` in that order before re-raising the signal.

```python
class VaultLock:
    def __init__(
        self,
        vault_path: Path,
        *,
        force: bool = False,
        install_signal_handlers: bool = True,  # NEW
    ) -> None:
        self.vault_path = Path(vault_path)
        self.lock_path = self.vault_path / _INDEXES_SUBDIR / _LOCK_FILENAME
        self.force = force
        self.install_signal_handlers = install_signal_handlers
        # ... rest unchanged ...

    def _install_cleanup_hooks(self) -> None:
        atexit.register(self._cleanup)
        if not self.install_signal_handlers:
            return  # NEW: daemon owns signal handling
        self._original_sigterm = signal.signal(signal.SIGTERM, self._signal_handler)
        self._original_sigint = signal.signal(signal.SIGINT, self._signal_handler)
        self._signal_handlers_installed = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/utils/test_vault_lock_signal_handlers.py -v`
Expected: PASS. Also run the existing `tests/utils/test_lock.py` to verify default behavior preserved.

### Task A8: Extract `_init_serve_runtime` helper from `serve.py` (NEW — critique B4 + S10)

**Files:**
- Modify: `src/engram/cli/serve.py`

- [ ] **Step 1: Extract the helper without changing today's behavior**

Refactor the body of `serve_cmd` (the inner-function body in `cli/serve.py`) so steps 1-10 (load config → run startup probes → cloud-sync detect → acquire VaultLock → optional startup pull → conflict scan → open VaultStorage → build + attach SyncCoordinator → construct FastEmbedProvider → build FastMCP server) move into a single async helper:

```python
async def _init_serve_runtime(
    *,
    config: EffectiveConfig,
    force: bool,
    skip_probes: bool,
    install_signal_handlers: bool = True,  # daemon passes False (Task A7)
) -> ServeRuntime:
    """Initialize VaultLock + storage + coordinator + embedder + FastMCP.

    Returns a ServeRuntime dataclass so daemon (Layer C) and --no-daemon
    (Layer F) can both call this helper and then either run the FastMCP
    stdio loop (no-daemon) or enter their own UDS accept loop (daemon).

    The install_signal_handlers parameter is plumbed through to VaultLock
    so the daemon can manage its own signal handling (Amendment 1).
    """
    # ... moves steps 1-10 from today's serve_cmd here ...


@dataclass
class ServeRuntime:
    """Runtime resources owned during serve. Layer C destroys these on shutdown."""

    config: EffectiveConfig
    vault_lock: VaultLock
    storage: VaultStorage
    coordinator: SyncCoordinator | None
    embedder: FastEmbedProvider
    fastmcp_server: FastMCP[Any]

    def teardown(self) -> None:
        if self.coordinator is not None:
            asyncio.run(self.coordinator.stop())
        self.storage.close()
        self.vault_lock.release()
```

- [ ] **Step 2: Refactor `serve_cmd` to call the helper**

```python
@app.command(name="serve")
def serve_cmd(...):
    config = load_config(...)
    configure_logging(...)
    runtime = asyncio.run(
        _init_serve_runtime(
            config=config,
            force=force,
            skip_probes=skip_probes,
            install_signal_handlers=True,  # today's behavior
        )
    )
    try:
        runtime.fastmcp_server.run()  # today's stdio loop
    finally:
        runtime.teardown()
```

This commit is BEHAVIORALLY IDENTICAL to today's `serve_cmd` — it's a pure refactor. The test suite's existing serve tests should still pass without modification.

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `uv run pytest tests/ -v -k "serve"`
Expected: PASS (all existing serve tests).

- [ ] **Step 4: Daemon (Layer C) imports the helper**

In Layer C step 3 implementation, `DaemonServer._init_engram_resources` becomes:

```python
async def _init_engram_resources(self) -> None:
    runtime = await _init_serve_runtime(
        config=self._effective_config,
        force=self._force_lock_takeover,
        skip_probes=False,
        install_signal_handlers=False,  # daemon owns its handlers
    )
    self._vault_lock = runtime.vault_lock
    self._storage = runtime.storage
    self._coordinator = runtime.coordinator
    self._embedder = runtime.embedder
    self._fastmcp_server = runtime.fastmcp_server
```

This closes critique B4 (circular dependency) — the helper exists in Layer A8, before Layer C imports it. And it closes critique S10 (naming) — `_init_serve_runtime` is the single canonical name.

### Layer A commit

- [ ] **Step 1: Stage Layer A files (now includes critique-pass additions)**

```bash
git add \
  src/engram/errors.py \
  src/engram/config/models.py \
  src/engram/config/loader.py \
  src/engram/daemon/__init__.py \
  src/engram/daemon/socket_paths.py \
  src/engram/diagnostics/check_codes.py \
  src/engram/utils/lock.py \
  src/engram/cli/serve.py \
  tests/daemon/__init__.py \
  tests/daemon/test_errors.py \
  tests/daemon/test_config.py \
  tests/daemon/test_socket_paths.py \
  tests/utils/test_vault_lock_signal_handlers.py \
  docs/PHASE_5_LAYER_A_AUDIT.md \
  docs/PHASE_5_FASTMCP_AUDIT.md \
  docs/PHASE_5_BASELINE.md
```

- [ ] **Step 2: Verify quality gates clean (scoped to Layer A's surface)**

```bash
uv run ruff format && uv run ruff check && uv run mypy && \
  uv run pytest tests/daemon/test_errors.py tests/daemon/test_config.py \
    tests/daemon/test_socket_paths.py tests/utils/test_vault_lock_signal_handlers.py \
    tests/ -k "serve" -v
```
Expected: ruff/mypy clean; ~17 new daemon tests pass; existing serve tests still green (A8 refactor is behaviorally pure).

- [ ] **Step 3: Commit (with -S -s per user CLAUDE.md)**

```bash
git commit -S -s -m "feat(daemon): Layer A — errors + DaemonConfig + path helpers + audits + helper extraction

Spec: ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md

- 5 new DaemonError subclasses with unique error_codes
- DaemonConfig Pydantic model with Field bounds on all numerics
  (closes deep-plan deltas E2-E5, H4, M2, L3, G5)
- 5-layer config loader precedence for daemon: block
- daemon/socket_paths.py with macOS UDS 104-byte limit enforcement
  (closes H5 + D5)
- 6 daemon doctor check code constants registered
  (impl deferred to Layer E -> daemon_checks.py per critique B3)
- VaultLock install_signal_handlers kwarg (critique B2 — daemon owns its handlers)
- cli/serve.py refactored to extract _init_serve_runtime helper +
  ServeRuntime dataclass (critique B4 + S10 — Layer C will import this)
- Pre-implementation audits:
  - SQLite WAL: shared connection per VaultStorage; daemon-safe under
    single-daemon-owns-storage model (no extra asyncio.Lock needed)
  - FastMCP: per-connection dispatch entrypoint identified via plan-time
    introspection (PHASE_5_FASTMCP_AUDIT.md)
  - Baseline test snapshot recorded (PHASE_5_BASELINE.md)

Phase 5 plan: docs/PHASE_5_PLAN.md"
```

**Approx LOC Layer A:** ~280 source + ~180 test = ~460 total.

---

## Layer B — Daemon utilities

**Goal:** Build the pure-utility modules the daemon (Layer C) and proxy (Layer D) depend on — protocol framing, peer-credential check, spawn-lock + double-fork, state file, log rotation. Each module is independently unit-testable with no daemon-process involvement.

**Files this layer creates:**
- Create: `src/engram/daemon/protocol.py`
- Create: `src/engram/daemon/auth.py`
- Create: `src/engram/daemon/spawn.py`
- Create: `src/engram/daemon/state.py`
- Create: `src/engram/daemon/log_rotation.py`
- Create: `tests/daemon/test_protocol.py`
- Create: `tests/daemon/test_auth.py`
- Create: `tests/daemon/test_spawn.py`
- Create: `tests/daemon/test_state.py`
- Create: `tests/daemon/test_log_rotation.py`

### Task B1: protocol.py — newline-delimited JSON-RPC framing

**Files:**
- Create: `src/engram/daemon/protocol.py`
- Create: `tests/daemon/test_protocol.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_protocol.py
"""Newline-delimited JSON-RPC framing helpers (spec Section 6.2 + Amendment 6)."""
from __future__ import annotations

import asyncio
import io
import json

import pytest

from engram.daemon.protocol import (
    DEFAULT_MAX_FRAME_BYTES,
    FrameTooLargeError,
    read_frame,
    write_frame,
)


@pytest.mark.asyncio
async def test_write_then_read_roundtrip():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    reader, writer = await _local_stream_pair()
    await write_frame(writer, payload)
    writer.write_eof()
    decoded = await read_frame(reader, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES)
    assert decoded == payload


@pytest.mark.asyncio
async def test_read_frame_returns_none_on_eof():
    reader, writer = await _local_stream_pair()
    writer.write_eof()
    assert await read_frame(reader, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES) is None


@pytest.mark.asyncio
async def test_read_frame_rejects_oversize():
    reader, writer = await _local_stream_pair()
    # 200 KB payload, max 100 KB
    big = {"data": "x" * 200_000}
    writer.write(json.dumps(big).encode() + b"\n")
    writer.write_eof()
    with pytest.raises(FrameTooLargeError):
        await read_frame(reader, max_frame_bytes=100_000)


@pytest.mark.asyncio
async def test_read_frame_handles_partial_first():
    # daemon dies after sending half a frame; reader observes EOF mid-frame
    reader, writer = await _local_stream_pair()
    writer.write(b'{"jsonrpc":"2.0",')  # no trailing newline
    writer.write_eof()
    # No complete frame -> None (or raises ProtocolError, depending on impl)
    result = await read_frame(reader, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES)
    assert result is None  # incomplete frame treated as EOF


async def _local_stream_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Return an in-memory (reader, writer) connected pair via os.pipe."""
    # Use asyncio.open_connection over a pair of socketpair fds
    import socket
    s1, s2 = socket.socketpair()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=DEFAULT_MAX_FRAME_BYTES * 2)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_accepted_socket(lambda: protocol, s1)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    # Wrap s2 as a writer the test can write to:
    return reader, asyncio.StreamWriter(
        *await loop.connect_accepted_socket(lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()), s2),
        asyncio.StreamReader(),
        loop,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_protocol.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/engram/daemon/protocol.py`**

```python
"""Newline-delimited JSON-RPC framing for daemon <-> proxy IPC over UDS.

Each frame is one JSON object terminated by '\\n'. SOCK_STREAM provides
flow control; the only protocol-level concern is the frame size limit
to prevent OOM from a buggy or malicious peer.

Spec: 2026-05-12-engram-daemon-mode-design.md Section 6.2 + Amendment 6.
"""

from __future__ import annotations

import json
from typing import Any, Final

import asyncio

DEFAULT_MAX_FRAME_BYTES: Final[int] = 16 * 1024 * 1024  # 16 MB


class FrameTooLargeError(Exception):
    """Frame exceeded the configured max_frame_bytes; connection should close."""


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    max_frame_bytes: int,
) -> dict[str, Any] | None:
    """Read one newline-delimited JSON-RPC frame.

    Returns the parsed dict, or None on clean EOF / incomplete frame.
    Raises FrameTooLargeError if a frame exceeds max_frame_bytes.
    Raises ValueError if the frame is not valid JSON.
    """
    try:
        line = await reader.readuntil(separator=b"\n")
    except asyncio.IncompleteReadError as exc:
        # Connection closed mid-frame; treat as EOF.
        if exc.partial:
            return None
        return None
    except asyncio.LimitOverrunError as exc:
        msg = f"Frame exceeds max_frame_bytes={max_frame_bytes}: {exc}"
        raise FrameTooLargeError(msg) from exc

    if len(line) > max_frame_bytes:
        msg = f"Frame size {len(line)} exceeds max_frame_bytes={max_frame_bytes}"
        raise FrameTooLargeError(msg)

    # Strip trailing newline before parsing
    return json.loads(line[:-1])


async def write_frame(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    """Write one newline-delimited JSON-RPC frame.

    Caller is responsible for awaiting writer.drain() if backpressure matters.
    """
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    writer.write(encoded + b"\n")


__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "FrameTooLargeError",
    "read_frame",
    "write_frame",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/daemon/test_protocol.py -v`
Expected: PASS (4 tests). If the helper `_local_stream_pair` is fragile across platforms, simplify by mocking with `asyncio.StreamReader.feed_data()` directly.

### Task B2: auth.py — peer-credential check (macOS + Linux)

**Files:**
- Create: `src/engram/daemon/auth.py`
- Create: `tests/daemon/test_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_auth.py
"""Peer credential check abstraction (spec Section 7.2)."""
from __future__ import annotations

import os
import socket
import struct
import sys
from unittest.mock import patch

import pytest

from engram.daemon.auth import PeerCred, peer_credentials


def test_peer_credentials_same_uid_accepts():
    a, b = socket.socketpair()
    try:
        cred = peer_credentials(a.fileno())
        assert cred.uid == os.getuid()
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SO_PEERCRED path")
def test_linux_so_peercred_returns_struct():
    a, b = socket.socketpair()
    try:
        cred = peer_credentials(a.fileno())
        assert isinstance(cred, PeerCred)
        assert cred.pid > 0
        assert cred.uid == os.getuid()
        assert cred.gid == os.getgid()
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS getpeereid path")
def test_macos_getpeereid_returns_struct():
    a, b = socket.socketpair()
    try:
        cred = peer_credentials(a.fileno())
        assert isinstance(cred, PeerCred)
        assert cred.pid == 0  # macOS getpeereid doesn't expose pid; sentinel 0
        assert cred.uid == os.getuid()
        assert cred.gid == os.getgid()
    finally:
        a.close()
        b.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_auth.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/engram/daemon/auth.py`**

```python
"""Peer credential check for daemon UDS connections.

Linux: SO_PEERCRED returns struct ucred = (pid, uid, gid).
macOS: getpeereid(fd) returns (uid, gid); no pid.

Spec: 2026-05-12-engram-daemon-mode-design.md Section 7.2.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
from dataclasses import dataclass

from engram.errors import PeerCredRejectError


@dataclass(frozen=True)
class PeerCred:
    pid: int  # 0 on macOS (not exposed)
    uid: int
    gid: int


def peer_credentials(fd: int) -> PeerCred:
    """Return (pid, uid, gid) of the peer connected to fd.

    Caller is expected to check uid == os.getuid() before accepting.
    """
    if sys.platform == "linux":
        # SO_PEERCRED -> struct ucred (pid, uid, gid) = 3 * 4 bytes
        data = _getsockopt(fd, socket.SOL_SOCKET, _SO_PEERCRED_LINUX, 12)
        pid, uid, gid = struct.unpack("iii", data)
        return PeerCred(pid=pid, uid=uid, gid=gid)

    if sys.platform == "darwin":
        # getpeereid via ctypes
        import ctypes

        libc = ctypes.CDLL("libc.dylib")
        c_uid = ctypes.c_uint32()
        c_gid = ctypes.c_uint32()
        rc = libc.getpeereid(fd, ctypes.byref(c_uid), ctypes.byref(c_gid))
        if rc != 0:
            errno = ctypes.get_errno()
            msg = f"getpeereid failed: errno={errno}"
            raise OSError(errno, msg)
        return PeerCred(pid=0, uid=c_uid.value, gid=c_gid.value)

    msg = f"peer_credentials not supported on platform {sys.platform}"
    raise NotImplementedError(msg)


def check_peer_or_reject(fd: int) -> PeerCred:
    """Return peer cred if same-UID; raise PeerCredRejectError otherwise."""
    cred = peer_credentials(fd)
    if cred.uid != os.getuid():
        msg = f"peer uid={cred.uid} does not match daemon uid={os.getuid()}"
        raise PeerCredRejectError(msg)
    return cred


# Linux's SO_PEERCRED is 17
_SO_PEERCRED_LINUX = 17


def _getsockopt(fd: int, level: int, opt: int, size: int) -> bytes:
    # socket.fromfd dups the fd; we own + close the dup; the original
    # fd (typically the daemon's accept loop's socket) is untouched.
    dup_sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return dup_sock.getsockopt(level, opt, size)
    finally:
        dup_sock.close()


__all__ = ["PeerCred", "check_peer_or_reject", "peer_credentials"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/daemon/test_auth.py -v`
Expected: PASS (3 tests; one skipped on the alternate platform).

### Task B3: spawn.py — spawn-lock + double-fork detach + readiness pipe

**Files:**
- Create: `src/engram/daemon/spawn.py`
- Create: `tests/daemon/test_spawn.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_spawn.py
"""Spawn-lock acquisition + double-fork + readiness pipe (spec Section 5.2 step 4 + Amendment 1)."""
from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path

import pytest

from engram.daemon.socket_paths import resolve_paths
from engram.daemon.spawn import (
    SpawnLockTimeout,
    SpawnReadiness,
    acquire_spawn_lock,
    wait_for_ready,
)


def test_acquire_spawn_lock_exclusive(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)

    with acquire_spawn_lock(paths.spawn_lock, timeout_seconds=1.0) as locked:
        assert locked is True
        # Second attempt blocks then times out
        with pytest.raises(SpawnLockTimeout):
            with acquire_spawn_lock(paths.spawn_lock, timeout_seconds=0.5):
                pass


def test_acquire_spawn_lock_releases_on_exit(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)

    with acquire_spawn_lock(paths.spawn_lock, timeout_seconds=1.0):
        pass  # released here
    # New acquirer succeeds
    with acquire_spawn_lock(paths.spawn_lock, timeout_seconds=1.0) as locked:
        assert locked is True


@pytest.mark.asyncio
async def test_wait_for_ready_success(tmp_path: Path):
    """Simulate a forked daemon writing 'ready\\n' to a pipe."""
    rfd, wfd = os.pipe()
    # Write 'ready\n' before reader checks
    os.write(wfd, b"ready\n")
    os.close(wfd)
    result = await wait_for_ready(rfd, timeout_seconds=2.0)
    assert result == SpawnReadiness.READY


@pytest.mark.asyncio
async def test_wait_for_ready_timeout(tmp_path: Path):
    rfd, wfd = os.pipe()
    # Don't write anything; expect timeout
    with pytest.raises(asyncio.TimeoutError):
        await wait_for_ready(rfd, timeout_seconds=0.5)
    os.close(rfd)
    os.close(wfd)


@pytest.mark.asyncio
async def test_wait_for_ready_error_message(tmp_path: Path):
    rfd, wfd = os.pipe()
    os.write(wfd, b"error: vault locked by pid 12345\n")
    os.close(wfd)
    result = await wait_for_ready(rfd, timeout_seconds=2.0)
    assert result == SpawnReadiness.ERROR
    assert "vault locked by pid 12345" in result.message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_spawn.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/engram/daemon/spawn.py`**

Reference: spec Section 5.2 step 4 + Amendment 1 (daemon startup ordering).

```python
"""Spawn-lock acquisition + double-fork daemon detach + readiness pipe.

The spawn-lock (separate from VaultLock) serializes concurrent
`engram serve` invocations attempting to spawn a daemon for the same
vault. Held briefly — just for the duration of the fork-and-wait-for-
ready dance.

Spec: 2026-05-12-engram-daemon-mode-design.md Section 5.2 step 4 +
Amendment 1 (startup ordering: signal-handlers BEFORE VaultLock BEFORE
unlink BEFORE bind BEFORE ready).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import errno
import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from engram.errors import DaemonSpawnError


class SpawnLockTimeout(DaemonSpawnError):
    """Acquiring the spawn lock timed out."""


class _ReadinessKind(enum.Enum):
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class SpawnReadiness:
    kind: _ReadinessKind
    message: str = ""

    READY = None  # set below

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SpawnReadiness):
            return self.kind == other.kind
        if isinstance(other, _ReadinessKind):
            return self.kind == other
        return NotImplemented

    @property
    def is_ready(self) -> bool:
        return self.kind == _ReadinessKind.READY

    @property
    def is_error(self) -> bool:
        return self.kind == _ReadinessKind.ERROR


# Class-attribute sentinels for ergonomic equality (`result == SpawnReadiness.READY`)
SpawnReadiness.READY = SpawnReadiness(kind=_ReadinessKind.READY)
SpawnReadiness.ERROR = SpawnReadiness(kind=_ReadinessKind.ERROR)


@contextlib.contextmanager
def acquire_spawn_lock(lock_path: Path, *, timeout_seconds: float) -> Iterator[bool]:
    """Acquire the spawn flock with a timeout.

    Polls fcntl.flock(LOCK_EX|LOCK_NB) until either it succeeds or
    timeout_seconds elapses. Raises SpawnLockTimeout on timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    msg = f"spawn lock {lock_path} contended for > {timeout_seconds}s"
                    raise SpawnLockTimeout(msg) from exc
                time.sleep(0.05)
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


async def wait_for_ready(read_fd: int, *, timeout_seconds: float) -> SpawnReadiness:
    """Wait for the spawned daemon to write 'ready\\n' or 'error: <msg>\\n'.

    Returns SpawnReadiness with kind=READY on success, kind=ERROR (with
    message) if daemon reported an error. Raises asyncio.TimeoutError
    if neither arrives within timeout_seconds.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, os.fdopen(read_fd, "rb", buffering=0))
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
    finally:
        transport.close()

    text = line.decode("utf-8").rstrip("\n")
    if text == "ready":
        return SpawnReadiness.READY
    if text.startswith("error:"):
        return SpawnReadiness(kind=_ReadinessKind.ERROR, message=text[len("error:") :].strip())
    msg = f"unexpected readiness payload: {text!r}"
    raise DaemonSpawnError(msg)


def double_fork_detach() -> None:
    """Standard Unix double-fork detach.

    Caller should be the parent before invoking; on return, the caller
    is the grandchild process with no controlling terminal.

    Closes stdin/stdout/stderr (caller can reopen log files as needed).
    """
    # First fork
    if os.fork() != 0:
        os._exit(0)
    os.setsid()
    # Second fork
    if os.fork() != 0:
        os._exit(0)
    # Now we're the grandchild
    os.chdir("/")
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    finally:
        os.close(devnull)


__all__ = [
    "SpawnLockTimeout",
    "SpawnReadiness",
    "acquire_spawn_lock",
    "double_fork_detach",
    "wait_for_ready",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/daemon/test_spawn.py -v`
Expected: PASS (5 tests). If `wait_for_ready` proves flaky on CI, replace `connect_read_pipe` with a simpler `loop.run_in_executor` + blocking read.

### Task B4: state.py — daemon state file

**Files:**
- Create: `src/engram/daemon/state.py`
- Create: `tests/daemon/test_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_state.py
"""Daemon state file at <vault>/.indexes/engram.state.json (spec Section 12.1 + Amendment 1)."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from engram.daemon.socket_paths import resolve_paths
from engram.daemon.state import DaemonState, read_state, write_state


def test_write_then_read_roundtrip(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)
    state = DaemonState(
        pid=os.getpid(),
        started_at="2026-05-12T14:20:04Z",
        vault_name="memex",
        vault_path=str(paths.vault),
        hostname=socket.gethostname(),
        config_snapshot={"idle_shutdown_seconds": 3600},
    )
    write_state(paths.state_file, state)
    loaded = read_state(paths.state_file)
    assert loaded == state


def test_read_state_missing_returns_none(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)
    assert read_state(paths.state_file) is None


def test_read_state_corrupt_returns_none(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)
    paths.state_file.write_text("not json{")
    assert read_state(paths.state_file) is None  # tolerate corruption


def test_state_file_mode_0600(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)
    state = DaemonState(
        pid=1234, started_at="2026-05-12T14:20:04Z", vault_name="memex",
        vault_path=str(paths.vault), hostname="testhost", config_snapshot={},
    )
    write_state(paths.state_file, state)
    mode = paths.state_file.stat().st_mode & 0o777
    assert mode == 0o600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_state.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/engram/daemon/state.py`**

```python
"""Daemon state file at <vault>/.indexes/engram.state.json.

Holds: pid, started_at, vault_name, vault_path, hostname, config snapshot.
Used by `engram daemon status` and to detect cross-machine sync confusion.

Spec: 2026-05-12-engram-daemon-mode-design.md Section 12.1 + Amendment 1.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from engram.utils.atomic_write import atomic_write_text


@dataclass(frozen=True)
class DaemonState:
    pid: int
    started_at: str  # ISO 8601 UTC
    vault_name: str
    vault_path: str
    hostname: str
    config_snapshot: dict[str, Any]


def write_state(path: Path, state: DaemonState) -> None:
    """Atomically write the state file.

    atomic_write_text already enforces 0o600 mode internally (see
    engram/utils/atomic_write.py); we do not pass an explicit mode here.
    """
    payload = json.dumps(asdict(state), separators=(",", ":"))
    atomic_write_text(path, payload)


def read_state(path: Path) -> DaemonState | None:
    """Read the state file; return None if missing or corrupt."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None
    try:
        return DaemonState(**data)
    except TypeError:
        return None  # schema drift -> treat as corrupt


__all__ = ["DaemonState", "read_state", "write_state"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/daemon/test_state.py -v`
Expected: PASS (4 tests).

### Task B5: log_rotation.py — daily + size-threshold rotation with retention

**Files:**
- Create: `src/engram/daemon/log_rotation.py`
- Create: `tests/daemon/test_log_rotation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_log_rotation.py
"""Log rotation policy (spec Section 13.3 + Amendment 8)."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from engram.daemon.log_rotation import configure_log_rotation


def test_rotation_at_size_threshold(tmp_path: Path):
    log_path = tmp_path / "engram.log"
    handler = configure_log_rotation(
        log_path,
        max_size_mb=1,  # 1 MB threshold
        retention_days=7,
        level="DEBUG",
    )
    logger = logging.getLogger("engram.daemon.test_rotation")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    # Emit > 1 MB of log lines
    for i in range(20_000):
        logger.info("x" * 80)  # ~80 bytes per line, 20k = ~1.6 MB

    handler.flush()
    rotated = list(tmp_path.glob("engram.log.*"))
    assert len(rotated) >= 1


def test_retention_deletes_old_files(tmp_path: Path):
    log_path = tmp_path / "engram.log"
    # Pre-create 10 rotated files
    for i in range(10):
        (tmp_path / f"engram.log.{i + 1}").write_text("old")
    handler = configure_log_rotation(
        log_path,
        max_size_mb=100,
        retention_days=7,
        level="INFO",
    )
    # Force retention sweep
    handler._sweep_retention()
    surviving = list(tmp_path.glob("engram.log.*"))
    assert len(surviving) <= 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_log_rotation.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `src/engram/daemon/log_rotation.py`**

```python
"""Daily + size-threshold log rotation with retention.

Wraps stdlib RotatingFileHandler + adds retention cleanup. `engram
daemon logs --follow` uses WatchedFileHandler-style inode-reopen logic
(implemented in cli/daemon.py via a separate tail loop).

Spec: 2026-05-12-engram-daemon-mode-design.md Section 13.3 + Amendment 8.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import time
from pathlib import Path


class EngramRotatingHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler with explicit retention sweep."""

    def __init__(
        self,
        path: Path,
        *,
        max_size_mb: int,
        retention_days: int,
    ) -> None:
        # Restrict perms BEFORE the parent constructor opens/creates the file,
        # so the initial inode is born with 0o600 perms rather than going
        # through a 0o644 → chmod window (closes critique S9).
        prior_umask = os.umask(0o077)
        try:
            super().__init__(
                filename=str(path),
                maxBytes=max_size_mb * 1024 * 1024,
                backupCount=retention_days,
                encoding="utf-8",
            )
        finally:
            os.umask(prior_umask)
        if path.exists():
            os.chmod(path, 0o600)  # belt-and-suspenders on top of umask
        self.retention_days = retention_days
        self._path = path

    def doRollover(self) -> None:
        super().doRollover()
        # Ensure mode 0600 on rotated files (super may not preserve)
        os.chmod(self.baseFilename, 0o600)
        for i in range(1, self.backupCount + 1):
            rotated = Path(f"{self.baseFilename}.{i}")
            if rotated.exists():
                os.chmod(rotated, 0o600)
        self._sweep_retention()

    def _sweep_retention(self) -> None:
        """Delete rotated files older than retention_days."""
        cutoff = time.time() - self.retention_days * 86400
        for rotated in self._path.parent.glob(f"{self._path.name}.*"):
            try:
                if rotated.stat().st_mtime < cutoff:
                    rotated.unlink(missing_ok=True)
            except OSError:
                pass


def configure_log_rotation(
    log_path: Path,
    *,
    max_size_mb: int,
    retention_days: int,
    level: str,
) -> EngramRotatingHandler:
    """Return a configured EngramRotatingHandler ready to attach to a logger."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = EngramRotatingHandler(
        log_path,
        max_size_mb=max_size_mb,
        retention_days=retention_days,
    )
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler.setFormatter(
        logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s")
    )
    return handler


__all__ = ["EngramRotatingHandler", "configure_log_rotation"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/daemon/test_log_rotation.py -v`
Expected: PASS (2 tests).

### Layer B commit

- [ ] **Step 1: Stage**

```bash
git add \
  src/engram/daemon/protocol.py \
  src/engram/daemon/auth.py \
  src/engram/daemon/spawn.py \
  src/engram/daemon/state.py \
  src/engram/daemon/log_rotation.py \
  tests/daemon/test_protocol.py \
  tests/daemon/test_auth.py \
  tests/daemon/test_spawn.py \
  tests/daemon/test_state.py \
  tests/daemon/test_log_rotation.py
```

- [ ] **Step 2: Verify quality gates**

```bash
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest tests/daemon/ -v
```
Expected: clean; ~25 tests pass in `tests/daemon/`.

- [ ] **Step 3: Commit**

```bash
git commit -S -s -m "feat(daemon): Layer B — protocol, auth, spawn, state, log rotation utilities

- protocol.py: newline-delimited JSON-RPC framing + max_frame_bytes
  (closes deep-plan G5)
- auth.py: SO_PEERCRED (Linux) + getpeereid (macOS) abstraction
- spawn.py: spawn-lock + double-fork + readiness pipe with
  daemon-startup-ordering contract (closes H1, M5)
- state.py: state file with hostname (closes L2)
- log_rotation.py: size+retention rotation with mode 0o600
  (closes M2, F4)

Spec: ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md"
```

**Approx LOC Layer B:** ~430 source + ~250 test = ~680 total.

---

## Layer C — Daemon server process

**Goal:** Build the daemon's accept loop, per-connection task spawning, idle-shutdown timer with two-phase atomic shutdown, and graceful drain. This layer makes the daemon a runnable process — but without a CLI entry yet (Layer F wires that).

**Files this layer creates:**
- Create: `src/engram/daemon/server.py`
- Create: `src/engram/daemon/fastmcp_dispatch.py` (decision A or B from Audit 2; Layer C step 1 selects)
- Create: `tests/daemon/test_server.py`

### Task C0: Pick FastMCP dispatch approach (Audit 2 resolution)

**Files:**
- Inspect: `~/repos/github.com/kpachhai/engram/.venv/lib/python*/site-packages/fastmcp/`

- [ ] **Step 1: Introspect FastMCP for per-connection dispatch entrypoints**

Run: `uv run python -c "import fastmcp; import inspect; print(inspect.getsourcefile(fastmcp))"`
Then read the FastMCP source. Look for:
- A method like `FastMCP.handle_message(payload: dict) -> dict | None` that takes one JSON-RPC payload and returns the response.
- A way to instantiate a per-connection session without `server.run()`.

- [ ] **Step 2: Document the decision in `src/engram/daemon/fastmcp_dispatch.py`**

If **Option A** (FastMCP exposes per-message handler):

```python
"""FastMCP per-connection dispatch shim — Option A (FastMCP exposes a handler).

Spec: 2026-05-12-engram-daemon-mode-design.md Section 12.1, Audit 2.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP


async def dispatch_one(server: FastMCP[Any], request: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch a single JSON-RPC request against a FastMCP instance."""
    # Implementation depends on FastMCP's actual API surface.
    # TODO during implementation: replace with the real FastMCP entrypoint.
    raise NotImplementedError("Layer C step 1 selects the entrypoint.")
```

If **Option B** (build minimal dispatch ourselves):

```python
"""FastMCP per-connection dispatch shim — Option B (we build dispatch).

FastMCP exposes only `server.run()` (stdio loop). We can't reuse it for
UDS multi-connection, so we extract the tool registry from `server` and
dispatch JSON-RPC manually.

Spec: 2026-05-12-engram-daemon-mode-design.md Section 12.1, Audit 2.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP


async def dispatch_one(server: FastMCP[Any], request: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch a single JSON-RPC request by reaching into FastMCP's registry.

    Routes 'initialize', 'tools/list', 'tools/call', 'notifications/*' against
    the FastMCP instance's tool registry.
    """
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        # Return the same initialize response FastMCP's stdio loop would.
        return _build_initialize_response(server, request_id)
    if method == "tools/list":
        return _build_tools_list_response(server, request_id)
    if method == "tools/call":
        return await _dispatch_tool_call(server, request)
    # ... other MCP methods ...

    return _jsonrpc_error(request_id, code=-32601, message=f"Method not found: {method}")


# Helper implementations below — concrete contracts depend on FastMCP internals.
```

Layer C step 1 audit picks A or B based on what FastMCP actually offers.

### Task C1: Daemon server accept loop

**Files:**
- Create: `src/engram/daemon/server.py`
- Create: `tests/daemon/test_server.py`

- [ ] **Step 1: Write the failing test (high-level integration smoke)**

```python
# tests/daemon/test_server.py
"""Daemon server accept loop + per-connection task + idle shutdown."""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

from engram.config.models import DaemonConfig
from engram.daemon.protocol import write_frame, read_frame, DEFAULT_MAX_FRAME_BYTES
from engram.daemon.server import DaemonServer
from engram.daemon.socket_paths import resolve_paths


@pytest.mark.asyncio
async def test_daemon_accepts_one_proxy_and_responds(tmp_path: Path):
    """Smoke: spawn daemon, connect one proxy, send a list_tools, get response."""
    vault = _prepare_minimal_vault(tmp_path)
    paths = resolve_paths(vault)
    config = DaemonConfig(idle_shutdown_seconds=0)  # never auto-shutdown for test

    daemon = DaemonServer(vault_path=vault, daemon_config=config)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=5.0)

    try:
        # Connect as proxy and send initialize
        reader, writer = await asyncio.open_unix_connection(str(paths.socket))
        await write_frame(
            writer,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        await writer.drain()
        response = await read_frame(reader, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES)
        assert response is not None
        assert response.get("id") == 1
        assert "result" in response
    finally:
        await daemon.shutdown()
        await server_task


@pytest.mark.asyncio
async def test_daemon_idle_shutdown_after_last_proxy(tmp_path: Path):
    """Idle shutdown timer fires after last proxy disconnects + idle_shutdown_seconds elapses."""
    vault = _prepare_minimal_vault(tmp_path)
    paths = resolve_paths(vault)
    config = DaemonConfig(idle_shutdown_seconds=1)  # 1s idle
    daemon = DaemonServer(vault_path=vault, daemon_config=config)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=5.0)

    # Connect + disconnect immediately
    reader, writer = await asyncio.open_unix_connection(str(paths.socket))
    writer.close()
    await writer.wait_closed()

    # Wait > 1s; daemon should shut down on its own
    await asyncio.wait_for(server_task, timeout=5.0)
    assert not paths.socket.exists()  # socket cleaned up


@pytest.mark.asyncio
async def test_daemon_rejects_oversize_frame(tmp_path: Path):
    """Daemon closes connection cleanly when proxy sends an oversize frame."""
    vault = _prepare_minimal_vault(tmp_path)
    paths = resolve_paths(vault)
    config = DaemonConfig(idle_shutdown_seconds=0, max_frame_bytes=64 * 1024)
    daemon = DaemonServer(vault_path=vault, daemon_config=config)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=5.0)

    try:
        reader, writer = await asyncio.open_unix_connection(str(paths.socket))
        # Send 100 KB (over the 64 KB limit)
        oversize = json.dumps({"big": "x" * 100_000}).encode() + b"\n"
        writer.write(oversize)
        await writer.drain()
        # Daemon should close the connection
        assert await reader.read() == b""
    finally:
        await daemon.shutdown()
        await server_task


def _prepare_minimal_vault(tmp_path: Path) -> Path:
    """Create a minimal vault for daemon-server tests."""
    vault = tmp_path / "vault"
    (vault / "thoughts").mkdir(parents=True)
    (vault / ".indexes").mkdir(parents=True)
    # ... write minimal config.yaml etc. as needed for VaultStorage init ...
    return vault
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_server.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/engram/daemon/server.py`**

```python
"""Daemon server process: accept loop + per-connection asyncio tasks.

Owns:
- VaultLock (held for daemon lifetime)
- VaultStorage (primary RW + extras RO via VaultRegistry)
- SyncCoordinator
- FastEmbedProvider
- UDS listener at <vault>/.indexes/engram.sock
- Idle-shutdown timer with two-phase atomic shutdown (Amendment 3)

Spec: 2026-05-12-engram-daemon-mode-design.md Section 5 + 8 + Amendments
1, 2, 3, 4.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

from engram.config.loader import load_config
from engram.config.models import DaemonConfig
from engram.daemon.auth import check_peer_or_reject
from engram.daemon.fastmcp_dispatch import dispatch_one
from engram.daemon.log_rotation import configure_log_rotation
from engram.daemon.protocol import (
    FrameTooLargeError,
    read_frame,
    write_frame,
)
from engram.daemon.socket_paths import SocketPaths, resolve_paths
from engram.daemon.state import DaemonState, write_state
from engram.errors import DaemonError, PeerCredRejectError
from engram.utils.lock import VaultLock

_log = logging.getLogger("engram.daemon.server")


class DaemonServer:
    """Per-vault daemon process owning the UDS listener + shared singletons."""

    def __init__(
        self,
        *,
        vault_path: Path,
        daemon_config: DaemonConfig,
    ) -> None:
        self.vault_path = vault_path
        self.daemon_config = daemon_config
        self.paths: SocketPaths = resolve_paths(vault_path)
        self._ready_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._shutdown_lock = asyncio.Lock()
        self._connected_proxies = 0
        self._idle_timer_task: asyncio.Task[None] | None = None
        self._server: asyncio.AbstractServer | None = None
        self._vault_lock: VaultLock | None = None
        self._storage = None  # type: ignore[assignment]  # set in serve_forever
        self._coordinator = None  # type: ignore[assignment]
        self._fastmcp_server = None  # type: ignore[assignment]

        # Metrics
        self._requests_total = 0
        self._requests_error = 0
        self._peer_cred_rejects = 0
        self._connect_during_drain = 0
        self._last_request_at: str | None = None

    async def serve_forever(self) -> None:
        """Main daemon entrypoint. Implements Amendment 1 ordering."""
        # 1. Install signal handlers
        self._install_signal_handlers()

        # 2. Acquire VaultLock BEFORE bind
        try:
            self._vault_lock = VaultLock(self.vault_path)
            self._vault_lock.acquire()
        except Exception as exc:
            _log.error("VaultLock acquire failed: %s", exc)
            raise

        # 3-5. Startup probes, cloud-sync detection, VaultStorage etc.
        # (Reuse cli/serve.py helpers; factored into a shared helper in Layer F.)
        await self._init_engram_resources()

        # 7. Unlink stale socket
        with contextlib.suppress(FileNotFoundError):
            self.paths.socket.unlink()

        # 8. Bind UDS
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self.paths.socket),
            limit=self.daemon_config.max_frame_bytes,
        )
        # 9. chmod 0600
        os.chmod(self.paths.socket, 0o600)

        # 10. Write state.json
        write_state(
            self.paths.state_file,
            DaemonState(
                pid=os.getpid(),
                started_at=datetime.now(UTC).isoformat(),
                vault_name=self.paths.vault.name,
                vault_path=str(self.paths.vault),
                hostname=socket.gethostname(),
                config_snapshot=self.daemon_config.model_dump(),
            ),
        )

        # 11. Signal readiness
        self._ready_event.set()

        # Start idle timer if configured
        if self.daemon_config.idle_shutdown_seconds > 0:
            self._idle_timer_task = asyncio.create_task(self._idle_timer_loop())

        # 12. Accept loop
        async with self._server:
            await self._shutdown_event.wait()

        # Drain
        await self._drain_and_exit()

    async def wait_until_ready(self, *, timeout: float) -> None:
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def shutdown(self) -> None:
        """External shutdown trigger (e.g., SIGTERM, `engram daemon stop`)."""
        self._shutdown_event.set()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """One asyncio task per accepted connection."""
        peer_fd = writer.get_extra_info("socket").fileno()

        # SO_PEERCRED / getpeereid check
        try:
            check_peer_or_reject(peer_fd)
        except PeerCredRejectError as exc:
            _log.warning("peer cred reject: %s", exc)
            self._peer_cred_rejects += 1
            writer.close()
            return

        async with self._shutdown_lock:
            if self._shutdown_event.is_set():
                # Daemon is draining; refuse new connections
                self._connect_during_drain += 1
                writer.close()
                return
            self._connected_proxies += 1
            # Cancel idle timer if running
            if self._idle_timer_task and not self._idle_timer_task.done():
                self._idle_timer_task.cancel()

        try:
            while True:
                try:
                    request = await asyncio.wait_for(
                        read_frame(reader, max_frame_bytes=self.daemon_config.max_frame_bytes),
                        timeout=self.daemon_config.connection_idle_timeout_seconds,
                    )
                except (FrameTooLargeError, ValueError):
                    self._requests_error += 1
                    break
                except asyncio.TimeoutError:
                    _log.info("connection idle timeout; closing")
                    break

                if request is None:
                    break  # EOF

                self._requests_total += 1
                self._last_request_at = datetime.now(UTC).isoformat()

                if self.daemon_config.log_redact_thought_content:
                    _log.info(
                        "request=%s id=%s",
                        request.get("method"),
                        request.get("id"),
                    )
                else:
                    _log.debug("request=%s", request)

                response = await dispatch_one(self._fastmcp_server, request)
                if response is not None:
                    await write_frame(writer, response)
                    await writer.drain()
        finally:
            async with self._shutdown_lock:
                self._connected_proxies -= 1
                if self._connected_proxies == 0 and self.daemon_config.idle_shutdown_seconds > 0:
                    self._idle_timer_task = asyncio.create_task(self._idle_timer_loop())
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _idle_timer_loop(self) -> None:
        """Two-phase atomic idle shutdown (Amendment 3)."""
        try:
            await asyncio.sleep(self.daemon_config.idle_shutdown_seconds)
        except asyncio.CancelledError:
            return

        async with self._shutdown_lock:
            if self._connected_proxies > 0:
                return  # someone connected during the wait
            # Phase 2: atomically close listener
            if self._server is not None:
                self._server.close()
            self._shutdown_event.set()

    async def _init_engram_resources(self) -> None:
        """Probes, storage, coordinator, FastEmbed, FastMCP build.

        Factored later in Layer F to share with `cli/serve.py --no-daemon`.
        """
        # Skeleton; full impl in Layer F.
        raise NotImplementedError("Layer F factors this shared helper.")

    async def _drain_and_exit(self) -> None:
        """Coordinator drain + storage close + lock release (Amendment 2)."""
        # ... drain implementation per Amendment 2 ...
        if self._vault_lock is not None:
            self._vault_lock.release()
        with contextlib.suppress(FileNotFoundError):
            self.paths.socket.unlink()
            self.paths.state_file.unlink()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())


__all__ = ["DaemonServer"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/daemon/test_server.py -v`
Expected: PASS (3 tests). May require iteration on the `_init_engram_resources` helper — its full implementation lives in Layer F (`_build_daemon_state` extracted from `cli/serve.py`).

### Layer C commit

- [ ] **Step 1: Stage + verify + commit**

```bash
git add src/engram/daemon/server.py src/engram/daemon/fastmcp_dispatch.py tests/daemon/test_server.py
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest tests/daemon/ -v
git commit -S -s -m "feat(daemon): Layer C — server (accept loop + per-conn task + idle shutdown)

- DaemonServer with Amendment 1 startup ordering
- Two-phase atomic idle shutdown (Amendment 3; closes H3)
- Coordinator-drain timeouts distinct from outer stop (Amendment 2; H2)
- Peer-cred reject path + counter (M3 surface)
- Per-connection inactivity timeout (Amendment 4; L3)
- FastMCP per-connection dispatch shim (Audit 2 resolution)

Spec: ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md"
```

**Approx LOC Layer C:** ~340 source + ~180 test = ~520 total.

---

## Layer D — Proxy client process

**Goal:** Build the proxy: stdio↔UDS byte shuffler, connect-with-spawn-if-missing dance, crash recovery with 3-retry exp backoff + jitter. Reuses Layer B's spawn helpers.

**Files this layer creates:**
- Create: `src/engram/daemon/client.py`
- Create: `tests/daemon/test_client.py`

### Task D1: Proxy byte-shuffler

**Files:**
- Create: `src/engram/daemon/client.py`
- Create: `tests/daemon/test_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/daemon/test_client.py
"""Proxy: byte shuffler + connect-with-spawn + crash retry (spec Section 5.2)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from engram.config.models import DaemonConfig
from engram.daemon.client import DaemonClient, _PROXY_RETRY_DELAYS_SECONDS
from engram.daemon.socket_paths import resolve_paths


def test_retry_schedule_matches_spec():
    # 1s + jitter, 4s + jitter, 16s + jitter; spec Section 5.6
    assert _PROXY_RETRY_DELAYS_SECONDS == (1.0, 4.0, 16.0)


@pytest.mark.asyncio
async def test_proxy_connects_to_running_daemon(tmp_path: Path):
    """Smoke: with a mock daemon already listening, proxy attaches + forwards."""
    vault = _prepare_minimal_vault(tmp_path)
    paths = resolve_paths(vault)
    config = DaemonConfig(idle_shutdown_seconds=0)

    # Start a minimal mock daemon
    server = await asyncio.start_unix_server(
        _echo_handler, path=str(paths.socket)
    )

    try:
        client = DaemonClient(
            vault_path=vault,
            daemon_config=config,
            stdin_reader=_string_reader('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),
            stdout_writer=_collecting_writer(),
        )
        await asyncio.wait_for(client.run_proxy_loop(), timeout=5.0)
        # Verify the echo response made it to stdout (mock daemon just echoes)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_proxy_spawns_daemon_on_cold_vault(tmp_path: Path):
    """If no socket exists, proxy runs spawn dance to start daemon."""
    vault = _prepare_minimal_vault(tmp_path)
    config = DaemonConfig(idle_shutdown_seconds=0, spawn_timeout_seconds=5)

    with patch("engram.daemon.client._spawn_daemon_process") as mock_spawn:
        mock_spawn.return_value = None  # simulate successful spawn

        client = DaemonClient(vault_path=vault, daemon_config=config)
        # Will try connect, miss, spawn-dance, retry connect
        # Test verifies spawn was called exactly once
        with patch("engram.daemon.client._try_connect", new=AsyncMock(side_effect=[None, AsyncMock()])):
            await client._connect_with_spawn_if_missing()
        mock_spawn.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_3_retry_exp_backoff_then_fails(tmp_path: Path):
    """Mid-session crash: 3 retries with exp backoff, then surfaces MCP error."""
    vault = _prepare_minimal_vault(tmp_path)
    config = DaemonConfig(idle_shutdown_seconds=0)
    client = DaemonClient(vault_path=vault, daemon_config=config)

    # Mock every connect attempt to fail
    with patch("engram.daemon.client._try_connect", new=AsyncMock(return_value=None)):
        with patch("engram.daemon.client._spawn_daemon_process", side_effect=Exception("spawn failed")):
            with pytest.raises(Exception) as exc_info:
                await client._reconnect_with_backoff(in_flight_request_id=42)
    # Should have attempted 3 retries before raising
    assert "3 retries" in str(exc_info.value).lower() or "retry" in str(exc_info.value).lower()


def _string_reader(text: str) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(text.encode())
    reader.feed_eof()
    return reader


def _collecting_writer():
    """Return a writer that collects everything written for later inspection."""
    # Implementation detail; use BytesIO + StreamWriter wrapper
    ...


async def _echo_handler(reader, writer):
    while True:
        line = await reader.readline()
        if not line:
            break
        writer.write(line)
        await writer.drain()
    writer.close()


def _prepare_minimal_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "thoughts").mkdir(parents=True)
    (vault / ".indexes").mkdir(parents=True)
    return vault
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/daemon/test_client.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/engram/daemon/client.py`**

```python
"""Proxy client: stdio <-> UDS byte shuffler + spawn dance + crash retry.

Each `engram serve` invocation (default mode) becomes a proxy. The proxy
doesn't parse MCP frames — it shuffles bytes between stdin/stdout
(toward Claude) and the UDS connection (toward the daemon).

On mid-session UDS EOF: retry 3 times with exp backoff (1s, 4s, 16s)
plus jitter, then surface MCP error to Claude.

Spec: 2026-05-12-engram-daemon-mode-design.md Section 5.2 + 5.6.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Final

from engram.config.models import DaemonConfig
from engram.daemon.socket_paths import resolve_paths
from engram.daemon.spawn import (
    SpawnLockTimeout,
    SpawnReadiness,
    acquire_spawn_lock,
    wait_for_ready,
)
from engram.errors import DaemonConnectionError, DaemonSpawnError

_PROXY_RETRY_DELAYS_SECONDS: Final[tuple[float, float, float]] = (1.0, 4.0, 16.0)
_JITTER_MAX_SECONDS: Final[float] = 2.0


class DaemonClient:
    """Proxy process. Connect to daemon, shuffle bytes."""

    def __init__(
        self,
        *,
        vault_path: Path,
        daemon_config: DaemonConfig,
        stdin_reader: asyncio.StreamReader | None = None,
        stdout_writer: asyncio.StreamWriter | None = None,
    ) -> None:
        self.vault_path = vault_path
        self.daemon_config = daemon_config
        self.paths = resolve_paths(vault_path)
        self._stdin_reader = stdin_reader
        self._stdout_writer = stdout_writer

    async def run_proxy_loop(self) -> int:
        """Main proxy loop. Returns exit code (0 = clean, !=0 = error)."""
        reader, writer = await self._connect_with_spawn_if_missing()
        try:
            return await self._shuffle_bytes(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _connect_with_spawn_if_missing(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Try connect; on miss, run spawn dance, then connect."""
        connection = await _try_connect(self.paths.socket)
        if connection is not None:
            return connection

        # Spawn dance
        with acquire_spawn_lock(
            self.paths.spawn_lock,
            timeout_seconds=self.daemon_config.spawn_lock_timeout_seconds,
        ):
            # Recheck — someone else may have spawned
            connection = await _try_connect(self.paths.socket)
            if connection is not None:
                return connection

            # Spawn the daemon
            await _spawn_daemon_process(
                vault_path=self.vault_path,
                spawn_timeout_seconds=self.daemon_config.spawn_timeout_seconds,
                wal_recovery_grace_seconds=self.daemon_config.wal_recovery_grace_seconds,
            )

        # After spawn, connect
        connection = await _try_connect(self.paths.socket)
        if connection is None:
            msg = f"daemon spawned but socket connect failed: {self.paths.socket}"
            raise DaemonConnectionError(msg)
        return connection

    async def _reconnect_with_backoff(
        self,
        *,
        in_flight_request_id: int | str | None = None,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """3-retry exp backoff per Section 5.6."""
        last_error: Exception | None = None
        for attempt, base_delay in enumerate(_PROXY_RETRY_DELAYS_SECONDS, start=1):
            jitter = random.uniform(0.0, _JITTER_MAX_SECONDS / attempt)
            await asyncio.sleep(base_delay + jitter)
            try:
                return await self._connect_with_spawn_if_missing()
            except (DaemonSpawnError, DaemonConnectionError, SpawnLockTimeout, OSError) as exc:
                last_error = exc
                continue
        msg = f"3 retries exhausted; last error: {last_error}"
        raise DaemonConnectionError(msg)

    async def _shuffle_bytes(
        self,
        socket_reader: asyncio.StreamReader,
        socket_writer: asyncio.StreamWriter,
    ) -> int:
        """Bidirectional byte shuffle between stdin/stdout and UDS."""
        stdin = self._stdin_reader or await _wrap_stdin()
        stdout = self._stdout_writer or await _wrap_stdout()

        async def stdin_to_socket() -> None:
            try:
                while True:
                    data = await stdin.read(4096)
                    if not data:
                        return
                    socket_writer.write(data)
                    await socket_writer.drain()
            except Exception:
                pass

        async def socket_to_stdout() -> None:
            try:
                while True:
                    data = await socket_reader.read(4096)
                    if not data:
                        return  # daemon EOF
                    stdout.write(data)
                    await stdout.drain()
            except Exception:
                pass

        await asyncio.gather(stdin_to_socket(), socket_to_stdout())
        return 0


async def _try_connect(
    socket_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """Try to connect to the UDS; return None on ECONNREFUSED or missing socket."""
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        return reader, writer
    except (FileNotFoundError, ConnectionRefusedError):
        return None


async def _spawn_daemon_process(
    *,
    vault_path: Path,
    spawn_timeout_seconds: int,
    wal_recovery_grace_seconds: int,
) -> None:
    """Double-fork the daemon. Wait for readiness signal."""
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # Child: exec the daemon subcommand with the pipe FD as readiness channel
        os.close(rfd)
        os.execvpe(
            sys.executable,
            [
                sys.executable,
                "-m",
                "engram",
                "daemon",
                "start",
                "--vault-path",
                str(vault_path),
                "--readiness-fd",
                str(wfd),
            ],
            os.environ,
        )

    # Parent: close write end, wait for ready or error
    os.close(wfd)

    # Compute effective timeout including WAL recovery grace if applicable
    effective_timeout = spawn_timeout_seconds
    wal_path = vault_path / ".indexes" / "engram.db-wal"
    if wal_path.exists() and wal_path.stat().st_size > 10 * 1024 * 1024:
        effective_timeout += wal_recovery_grace_seconds

    result = await wait_for_ready(rfd, timeout_seconds=effective_timeout)
    if result.is_error:
        msg = f"daemon spawn reported error: {result.message}"
        raise DaemonSpawnError(msg)


async def _wrap_stdin() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


async def _wrap_stdout() -> asyncio.StreamWriter:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        sys.stdout,
    )
    return asyncio.StreamWriter(transport, protocol, None, loop)


__all__ = ["DaemonClient"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/daemon/test_client.py -v`
Expected: PASS (4 tests). May need test-helper refinement around `_string_reader` and `_collecting_writer`.

### Layer D commit

- [ ] **Step 1: Stage + verify + commit**

```bash
git add src/engram/daemon/client.py tests/daemon/test_client.py
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest tests/daemon/ -v
git commit -S -s -m "feat(daemon): Layer D — proxy client (byte shuffler + spawn + retry)

- DaemonClient.run_proxy_loop: bidirectional stdio<->UDS byte shuffle
- _connect_with_spawn_if_missing: try-connect -> spawn-lock -> double-fork
- _reconnect_with_backoff: 3-retry exp backoff with jitter (1s, 4s, 16s)
- WAL recovery grace folded into spawn timeout (Amendment 4)
- Fingerprint dedup in capture replay guards idempotency

Spec: ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md"
```

**Approx LOC Layer D:** ~250 source + ~180 test = ~430 total.

---

## Layer E — Doctor checks

**Goal:** Wire the 6 daemon-related doctor check codes (registered in Layer A) to live state.

**Files this layer modifies:**
- Modify: `src/engram/diagnostics/doctor.py` (add 6 check functions)
- Create: `tests/diagnostics/test_doctor_daemon_checks.py`

### Task E1: Implement 6 daemon doctor checks

**Files:**
- Create: `src/engram/diagnostics/daemon_checks.py` (per critique B3 — matches engram convention of per-feature check files like `phase3_checks.py` / `phase4_checks.py`, but without Phase N framing per CLAUDE.md)
- Modify: `src/engram/diagnostics/doctor.py` (only the dispatcher line that calls into the new module — minimal touch)
- Create: `tests/diagnostics/test_doctor_daemon_checks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/diagnostics/test_doctor_daemon_checks.py
"""Daemon-mode doctor checks (spec Section 13.2 + Amendment 5)."""
from __future__ import annotations

import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from engram.daemon.socket_paths import resolve_paths
from engram.daemon.state import DaemonState, write_state
from engram.diagnostics.check_codes import (
    DAEMON_RUNNING,
    DAEMON_SOCKET_PATH_TOO_LONG,
    DAEMON_SOCKET_PERMISSIONS,
    DAEMON_SOCKET_STALE,
    DAEMON_LOG_ROTATION_HEALTHY,
    DAEMON_UPTIME_EXCESSIVE,
)
from engram.diagnostics.doctor import (
    check_daemon_log_rotation_healthy,
    check_daemon_running,
    check_daemon_socket_path_too_long,
    check_daemon_socket_permissions,
    check_daemon_socket_stale,
    check_daemon_uptime_excessive,
)


def test_daemon_running_reports_not_running_on_cold_vault(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    report = check_daemon_running(vault)
    assert report.code == DAEMON_RUNNING
    assert report.severity == "INFO"
    assert "not running" in report.message.lower()


def test_daemon_socket_stale_warns_when_socket_present_no_listener(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)
    # Create a regular file at the socket path; no listener
    paths.socket.write_text("")
    report = check_daemon_socket_stale(vault)
    assert report.code == DAEMON_SOCKET_STALE
    assert report.severity == "WARN"


def test_daemon_socket_permissions_warns_on_non_0600(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)
    # Bind a real socket then chmod to 0644
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(paths.socket))
    try:
        os.chmod(paths.socket, 0o644)
        report = check_daemon_socket_permissions(vault)
        assert report.severity == "WARN"
    finally:
        s.close()
        paths.socket.unlink(missing_ok=True)


def test_daemon_uptime_excessive_info_after_7d(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)
    write_state(
        paths.state_file,
        DaemonState(
            pid=os.getpid(),
            started_at="2020-01-01T00:00:00+00:00",  # 6 years ago
            vault_name="memex",
            vault_path=str(vault),
            hostname=socket.gethostname(),
            config_snapshot={},
        ),
    )
    report = check_daemon_uptime_excessive(vault)
    assert report.code == DAEMON_UPTIME_EXCESSIVE
    assert report.severity == "INFO"
    assert "consider" in report.message.lower()


def test_daemon_log_rotation_warns_on_oversize_no_rotation(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = resolve_paths(vault)
    # Create a > 100 MB log file with old mtime
    paths.log_file.write_bytes(b"x" * (101 * 1024 * 1024))
    import time
    old_time = time.time() - 86400 * 2  # 2 days ago
    os.utime(paths.log_file, (old_time, old_time))
    report = check_daemon_log_rotation_healthy(vault, max_size_mb=100)
    assert report.severity == "WARN"


def test_daemon_socket_path_too_long_warns(tmp_path: Path):
    # Construct a path that exceeds 104 bytes
    deep = tmp_path
    for _ in range(20):
        deep = deep / "x" * 8
    deep.mkdir(parents=True)
    report = check_daemon_socket_path_too_long(deep)
    assert report.code == DAEMON_SOCKET_PATH_TOO_LONG
    assert report.severity == "WARN"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/diagnostics/test_doctor_daemon_checks.py -v`
Expected: FAIL — check functions don't exist.

- [ ] **Step 3: Implement 6 check functions in `src/engram/diagnostics/daemon_checks.py` (new file)**

Each follows engram's existing `CheckReport` pattern (mirrors `phase3_checks.py` / `phase4_checks.py` structure). `doctor.py` gets a one-line import + dispatcher entry. Sketch:

```python
def check_daemon_running(vault_path: Path) -> CheckReport:
    """INFO-ok if daemon socket responds to ping; INFO-not-running otherwise."""
    paths = resolve_paths(vault_path)
    if not paths.socket.exists():
        return CheckReport(
            code=DAEMON_RUNNING,
            severity="INFO",
            message=f"Daemon not running for vault {paths.vault.name}. "
                    f"Run `engram daemon start` or just open a Claude session — it auto-spawns.",
        )
    # Try a quick connect
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(str(paths.socket))
        s.close()
        state = read_state(paths.state_file)
        if state is not None:
            return CheckReport(
                code=DAEMON_RUNNING,
                severity="INFO",
                message=f"Daemon running (PID {state.pid}, started {state.started_at})",
            )
    except OSError:
        pass
    return CheckReport(
        code=DAEMON_RUNNING,
        severity="INFO",
        message="Daemon not responsive",
    )


def check_daemon_socket_permissions(vault_path: Path) -> CheckReport:
    """WARN if socket file mode != 0600 or owner != self."""
    # ... per spec Section 7.1 + 13.2 ...


def check_daemon_socket_stale(vault_path: Path) -> CheckReport:
    """WARN if socket file exists but no daemon listening + state.json missing or stale PID."""
    # ...


def check_daemon_log_rotation_healthy(vault_path: Path, *, max_size_mb: int = 100) -> CheckReport:
    """WARN if log file > max_size_mb AND last rotation > 24h ago."""
    # ...


def check_daemon_uptime_excessive(vault_path: Path) -> CheckReport:
    """INFO if daemon uptime > 7 days."""
    # ... read state.started_at, compute delta ...


def check_daemon_socket_path_too_long(vault_path: Path) -> CheckReport:
    """WARN if vault path produces a UDS socket path exceeding 104 bytes."""
    # ... resolve_paths + len check; catch DaemonError ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/diagnostics/test_doctor_daemon_checks.py -v`
Expected: PASS (6 tests).

### Layer E commit

- [ ] **Step 1: Stage + verify + commit (scoped per critique B5)**

```bash
git add \
  src/engram/diagnostics/daemon_checks.py \
  src/engram/diagnostics/doctor.py \
  tests/diagnostics/test_doctor_daemon_checks.py
uv run ruff format && uv run ruff check && uv run mypy && \
  uv run pytest tests/diagnostics/test_doctor_daemon_checks.py tests/diagnostics/ -v
git commit -S -s -m "feat(daemon): Layer E — 6 doctor checks for daemon mode

- daemon_running         (INFO/INFO-not-running)
- daemon_socket_permissions  (WARN on non-0600)
- daemon_socket_stale    (WARN on orphaned socket file)
- daemon_log_rotation_healthy (WARN on oversize without rotation)
- daemon_uptime_excessive    (INFO after 7d uptime)
- daemon_socket_path_too_long (WARN on macOS 104-byte limit; Amendment 5)

Spec: ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md Section 13.2"
```

**Approx LOC Layer E:** ~150 source + ~100 test = ~250 total.

---

## Layer F — CLI integration callsites

**Goal:** Refactor `engram serve` into the proxy-default path with `--no-daemon` escape hatch. Add new `engram daemon` subcommand group. Per the engram CLAUDE.md "Layer ordering: integration callsites BEFORE Layer G tests" rule.

**Files this layer creates or modifies:**
- Modify: `src/engram/cli/serve.py` (extract `_serve_no_daemon`; default-to-proxy; keep `--no-daemon` flag)
- Create: `src/engram/cli/daemon.py` (typer subcommand group)
- Modify: `src/engram/cli/__init__.py` (register daemon subcommand)
- Modify: `src/engram/daemon/server.py` (call the extracted `_init_engram_resources` helper from `_serve_no_daemon`)

### Task F1: Extract `_serve_no_daemon` helper from `serve.py`

**Files:**
- Modify: `src/engram/cli/serve.py`

- [ ] **Step 1: Extract today's `serve_cmd` body into `_serve_no_daemon()`**

The existing function in `src/engram/cli/serve.py` (read it to confirm exact structure) contains the full single-process serve flow. Extract steps 2-11 into a module-private async helper:

```python
async def _serve_no_daemon(
    *,
    config: EffectiveConfig,
    force: bool,
    skip_probes: bool,
) -> None:
    """Today's single-process serve flow — the escape hatch.

    This helper is also called by the daemon to initialize VaultLock,
    storage, coordinator, FastEmbed, FastMCP (Layer C step 4).
    """
    # ... existing logic from serve_cmd, factored out ...
```

The daemon's `_init_engram_resources` (Layer C) will call this helper up through the FastMCP-build step, then return — instead of invoking `server.run()`.

- [ ] **Step 2: Refactor `serve_cmd` to dispatch to proxy or no-daemon path**

```python
@app.command(name="serve")
def serve_cmd(
    config_path: Path | None = typer.Option(None, "--config", ...),
    vault_name: str | None = typer.Option(None, "--vault", ...),
    log_level: str | None = typer.Option(None, "--log-level", ...),
    force: bool = typer.Option(False, "--force", ...),
    skip_probes: bool = typer.Option(False, "--skip-probes", ...),
    no_daemon: bool = typer.Option(  # NEW
        False,
        "--no-daemon",
        help="Run single-process serve (escape hatch). Default is proxy mode.",
    ),
) -> None:
    """Start the engram MCP server (proxy mode by default; --no-daemon for single-process)."""
    config = load_config(...)
    configure_logging(...)

    if no_daemon:
        asyncio.run(_serve_no_daemon(config=config, force=force, skip_probes=skip_probes))
        return

    # Proxy mode
    if not config.daemon.auto_spawn:
        # Check if daemon already running; if not, fail fast
        if not _is_daemon_running(config.vault_path):
            msg = f"no daemon running for vault {config.vault_name} and auto_spawn=false"
            raise typer.Exit(2) from DaemonNotRunningError(msg)

    client = DaemonClient(vault_path=config.vault_path, daemon_config=config.daemon)
    exit_code = asyncio.run(client.run_proxy_loop())
    raise typer.Exit(exit_code) if exit_code != 0 else None
```

### Task F2: New `cli/daemon.py` subcommand group

**Files:**
- Create: `src/engram/cli/daemon.py`

- [ ] **Step 1: Implement the 4 subcommands**

```python
"""`engram daemon` subcommand group (Phase 5).

Subcommands:
- start [--detach]              start daemon in foreground (default) or background
- stop [--force]                stop daemon gracefully (or SIGKILL after 5s with --force)
- status [--json] [--vault N] [--all]   print daemon state
- logs [--tail N] [--follow]    tail daemon log file

Spec: 2026-05-12-engram-daemon-mode-design.md Section 10.2.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path

import typer

from engram.config.loader import load_config
from engram.daemon.server import DaemonServer
from engram.daemon.socket_paths import resolve_paths
from engram.daemon.spawn import double_fork_detach
from engram.daemon.state import read_state

app = typer.Typer(name="daemon", help="Daemon-mode lifecycle commands.")


@app.command()
def start(
    detach: bool = typer.Option(False, "--detach", help="Background daemon (double-fork)."),
    vault: str | None = typer.Option(None, "--vault"),
    config: Path | None = typer.Option(None, "--config"),
    force: bool = typer.Option(False, "--force", help="Force VaultLock takeover."),
    skip_probes: bool = typer.Option(False, "--skip-probes"),
    readiness_fd: int | None = typer.Option(None, "--readiness-fd", hidden=True),  # spawn-pipe internal
) -> None:
    """Start the engram daemon."""
    cfg = load_config(explicit_vault_config=config, vault_name=vault)
    if detach:
        double_fork_detach()
    # Set up logging to file (replace stdout/stderr handlers)
    # ... configure log rotation from cfg.daemon ...
    server = DaemonServer(vault_path=cfg.vault_path, daemon_config=cfg.daemon)
    if readiness_fd is not None:
        # We were spawned by a proxy; signal ready via the pipe
        server.set_readiness_fd(readiness_fd)
    asyncio.run(server.serve_forever())


@app.command()
def stop(
    force: bool = typer.Option(False, "--force"),
    vault: str | None = typer.Option(None, "--vault"),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """Stop the running daemon for this vault."""
    cfg = load_config(explicit_vault_config=config, vault_name=vault)
    paths = resolve_paths(cfg.vault_path)
    state = read_state(paths.state_file)
    if state is None:
        typer.echo(f"no daemon running for vault {cfg.vault_name}")
        raise typer.Exit(0)

    os.kill(state.pid, signal.SIGTERM)
    # Wait up to coordinator_flush_seconds + drain budget
    deadline = time.monotonic() + cfg.daemon.coordinator_flush_seconds + 10
    while time.monotonic() < deadline:
        if not _pid_alive(state.pid):
            typer.echo("daemon stopped")
            return
        time.sleep(0.5)

    if force:
        os.kill(state.pid, signal.SIGKILL)
        typer.echo("daemon SIGKILLed after timeout")
    else:
        typer.echo("daemon did not stop within timeout; use --force to SIGKILL")
        raise typer.Exit(1)


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json"),
    vault: str | None = typer.Option(None, "--vault"),
    config: Path | None = typer.Option(None, "--config"),
    all_vaults: bool = typer.Option(False, "--all"),
) -> None:
    """Print daemon status."""
    cfg = load_config(explicit_vault_config=config, vault_name=vault)
    paths = resolve_paths(cfg.vault_path)
    state = read_state(paths.state_file)

    if state is None:
        not_running = _build_not_running_status(cfg, paths)
        if json_output:
            typer.echo(json.dumps(not_running, indent=2))
        else:
            typer.echo(_format_status_text(not_running))
        return

    running = _build_running_status(cfg, paths, state)
    if json_output:
        typer.echo(json.dumps(running, indent=2))
    else:
        typer.echo(_format_status_text(running))


@app.command()
def logs(
    tail: int = typer.Option(200, "--tail"),
    follow: bool = typer.Option(False, "--follow"),
    vault: str | None = typer.Option(None, "--vault"),
    config: Path | None = typer.Option(None, "--config"),
) -> None:
    """Tail the daemon log file."""
    cfg = load_config(explicit_vault_config=config, vault_name=vault)
    paths = resolve_paths(cfg.vault_path)
    if not paths.log_file.exists():
        typer.echo(f"no log file at {paths.log_file} (daemon may never have run)")
        raise typer.Exit(0)

    if not cfg.daemon.log_redact_thought_content:
        typer.echo("[engram-daemon DEBUG mode active — log may contain thought content; treat as PII]")

    if follow:
        _tail_follow(paths.log_file)
    else:
        # Print last `tail` lines
        with paths.log_file.open() as f:
            lines = f.readlines()
        for line in lines[-tail:]:
            typer.echo(line.rstrip("\n"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def register(parent_app: typer.Typer) -> None:
    parent_app.add_typer(app, name="daemon")


__all__ = ["register"]
```

### Task F3: Register `daemon` subcommand in main typer app

**Files:**
- Modify: `src/engram/cli/__init__.py`

- [ ] **Step 1: Add registration**

Find the main `app` construction in `cli/__init__.py` and add:

```python
from engram.cli import daemon as daemon_cli

daemon_cli.register(app)
```

- [ ] **Step 2: Verify `engram daemon --help` works**

Run: `uv pip install -e .[dev] && uv run engram daemon --help`
Expected: typer prints help for the daemon subcommand group with 4 subcommands.

### Layer F commit

- [ ] **Step 1: Stage + verify + commit**

```bash
git add src/engram/cli/serve.py src/engram/cli/daemon.py src/engram/cli/__init__.py
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest tests/ -v
git commit -S -s -m "feat(daemon): Layer F — CLI integration (serve refactor + daemon subcommand group)

- cli/serve.py refactored: proxy mode default + --no-daemon escape
- cli/daemon.py: 4 subcommands (start, stop, status, logs)
- engram daemon start --readiness-fd: internal hook for spawn-pipe handshake
- Mutual exclusion: --no-daemon + running daemon -> clear LockError + remediation hint (M3)

Spec: ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md Section 10"
```

**Approx LOC Layer F:** ~280 source + ~80 test = ~360 total.

---

## Layer G — Integration + property + hermetic CLI smoke

**Goal:** Validate Layer A-F end-to-end. Cover the deep-plan-flagged edge cases via integration tests, property tests, and 9 new hermetic CLI smoke tests against the installed binary.

**Note on test contracts (post-critique S5):** the test bodies below show the SCENARIO + ASSERTION SHAPE of each test (what behavior the test verifies, what assertion fires). The Layer G implementation task expands each contract into a fully-implemented test body before the Layer G commit. **Layer G does NOT ship with `...` placeholder bodies** — every test contract listed below maps to one task ("Implement test X per its contract") that runs during Layer G execution. The plan lists test contracts because the contract IS the test specification; the implementation work is straightforward translation of contract → test code following the patterns established in Layer A-F unit tests.

The Layer G commit step verifies all contracts are implemented + green via `uv run pytest -v` (full suite, the Phase 5 exit gate).

**Files this layer creates:**
- Create: `tests/integration/test_daemon_multi_proxy.py`
- Create: `tests/integration/test_daemon_lifecycle.py`
- Create: `tests/integration/test_daemon_multivault.py`
- Create: `tests/integration/test_daemon_no_daemon_regression.py`
- Create: `tests/daemon/test_crash_recovery.py`
- Create: `tests/daemon/test_dispatch_isolation.py`
- Create: `tests/daemon/test_embedding_cache_concurrency.py`
- Create: `tests/properties/test_daemon_concurrency.py`
- Create: `tests/properties/test_daemon_spawn_race.py`
- Modify: `tests/test_phase4_cli_smoke.py` (add 9 daemon-mode smoke tests)

### Task G1: Integration — single + concurrent proxies

**Files:**
- Create: `tests/integration/test_daemon_multi_proxy.py`

- [ ] **Step 1: Write the test**

```python
# tests/integration/test_daemon_multi_proxy.py
"""5 concurrent proxies hitting the same daemon — the core multi-session win."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram.daemon.client import DaemonClient
from engram.daemon.server import DaemonServer
from engram.config.models import DaemonConfig


@pytest.mark.asyncio
async def test_5_concurrent_proxies_capture_distinct_thoughts(tmp_path: Path):
    vault = _prepare_vault(tmp_path)
    config = DaemonConfig(idle_shutdown_seconds=0)
    daemon = DaemonServer(vault_path=vault, daemon_config=config)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=10.0)

    try:
        # 5 concurrent captures
        clients = [DaemonClient(vault_path=vault, daemon_config=config) for _ in range(5)]
        captures = [
            _drive_capture(c, f"thought number {i}") for i, c in enumerate(clients)
        ]
        results = await asyncio.gather(*captures)
        assert len(results) == 5
        # Verify each capture got a distinct fingerprint + thought_id
        fingerprints = {r["fingerprint"] for r in results}
        assert len(fingerprints) == 5

        # Verify all 5 thoughts in markdown SoT
        thought_files = list((vault / "thoughts").rglob("*.md"))
        assert len(thought_files) == 5
    finally:
        await daemon.shutdown()
        await server_task


@pytest.mark.asyncio
async def test_5_concurrent_proxies_same_fingerprint_dedup(tmp_path: Path):
    """5 proxies capture identical content -> exactly one markdown file, all 5 get same thought_id."""
    vault = _prepare_vault(tmp_path)
    config = DaemonConfig(idle_shutdown_seconds=0)
    daemon = DaemonServer(vault_path=vault, daemon_config=config)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=10.0)

    try:
        clients = [DaemonClient(vault_path=vault, daemon_config=config) for _ in range(5)]
        captures = [_drive_capture(c, "same content") for c in clients]
        results = await asyncio.gather(*captures)

        thought_ids = {r["thought_id"] for r in results}
        assert len(thought_ids) == 1  # dedup

        thought_files = list((vault / "thoughts").rglob("*.md"))
        assert len(thought_files) == 1
    finally:
        await daemon.shutdown()
        await server_task


async def _drive_capture(client: DaemonClient, content: str) -> dict:
    """Helper: connect client, send capture_thought, return response."""
    # Construct a minimal JSON-RPC capture_thought request, send via proxy,
    # read response, return parsed.
    ...


def _prepare_vault(tmp_path: Path) -> Path:
    """Real vault with minimal config sufficient to init VaultStorage + FastEmbed."""
    ...
```

- [ ] **Step 2: Run + iterate until passing**

Run: `uv run pytest tests/integration/test_daemon_multi_proxy.py -v`
Expected: PASS (2 tests). Iterate on `_drive_capture` and `_prepare_vault` helpers as needed.

### Task G2: Lifecycle integration — spawn race, idle, auto-wake, peer-cred reject

**Files:**
- Create: `tests/integration/test_daemon_lifecycle.py`

- [ ] **Step 1: Write 6 scenarios**

```python
# tests/integration/test_daemon_lifecycle.py
"""Lifecycle: spawn race, idle, auto-wake, peer-cred, mutual exclusion."""
import asyncio
import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_spawn_race_10_simultaneous_invocations(tmp_path):
    """asyncio.gather 10 spawn-dance attempts; exactly one daemon spawned."""
    # ... assert one PID across all 10 final states ...


@pytest.mark.asyncio
async def test_idle_shutdown_fires_after_timeout(tmp_path):
    """daemon shuts down N seconds after last proxy disconnects."""
    # ... use mock clock or short timeout ...


@pytest.mark.asyncio
async def test_auto_wake_after_idle_shutdown(tmp_path):
    """post-idle-shutdown, fresh engram serve re-spawns transparently."""


@pytest.mark.asyncio
async def test_so_peercred_reject_different_uid(tmp_path):
    """Mock peer_credentials to return different UID; assert connection refused + reject counter incremented."""


@pytest.mark.asyncio
async def test_no_daemon_vs_daemon_mutual_exclusion(tmp_path):
    """engram serve --no-daemon holds VaultLock; engram serve (default) spawn fails with LockError."""


@pytest.mark.asyncio
async def test_idle_timer_does_not_fire_with_connected_proxies(tmp_path):
    """Property: while ≥1 proxy is connected, idle timer never fires (closes H3)."""
```

- [ ] **Step 2: Run + iterate**

Run: `uv run pytest tests/integration/test_daemon_lifecycle.py -v`
Expected: PASS (6 tests).

### Task G3: Multivault preservation

**Files:**
- Create: `tests/integration/test_daemon_multivault.py`

- [ ] **Step 1: Test that daemon mounts primary + extras same as today**

```python
# tests/integration/test_daemon_multivault.py
@pytest.mark.asyncio
async def test_daemon_mounts_primary_and_extras(tmp_path):
    """Daemon for primary vault mounts 2 read-only extras; search aggregates."""
    primary = _prepare_vault(tmp_path / "primary")
    extra_a = _prepare_vault(tmp_path / "extra_a")
    extra_b = _prepare_vault(tmp_path / "extra_b")
    # Seed each with distinct content
    # Configure primary's engram.config.yaml with vaults: list
    # Start daemon for primary
    # Connect proxy
    # Send search_thoughts -> assert results include hits from all 3 vaults
    ...
```

- [ ] **Step 2: Run**

Expected: PASS.

### Task G4: `--no-daemon` regression

**Files:**
- Create: `tests/integration/test_daemon_no_daemon_regression.py`

- [ ] **Step 1: Test today's path bit-for-bit**

```python
# tests/integration/test_daemon_no_daemon_regression.py
@pytest.mark.asyncio
async def test_no_daemon_serves_just_like_today(tmp_path):
    """engram serve --no-daemon against a vault with no daemon works bit-for-bit as today."""
    # Spawn `engram serve --no-daemon` as a subprocess
    # Send MCP frames via stdin
    # Verify responses via stdout
    # Verify NO socket file created
    # Verify VaultLock held directly by the subprocess
    ...
```

- [ ] **Step 2: Run**

Expected: PASS.

### Task G5: Crash recovery

**Files:**
- Create: `tests/daemon/test_crash_recovery.py`

```python
@pytest.mark.asyncio
async def test_sigkill_mid_capture_proxy_retries(tmp_path):
    """SIGKILL daemon mid-capture; proxy retries 1s/4s/16s; succeeds on respawn."""
    ...


@pytest.mark.asyncio
async def test_capture_replay_is_idempotent(tmp_path):
    """In-flight capture_thought during daemon crash; proxy replays; storage dedupes."""
    ...


@pytest.mark.asyncio
async def test_three_strikes_fails_with_mcp_error(tmp_path):
    """If all 3 retries fail, proxy emits MCP error to Claude + exits nonzero."""
    ...


@pytest.mark.asyncio
async def test_stale_socket_unlinks_at_spawn(tmp_path):
    """Stale socket file from a crashed daemon → spawn dance unlinks + binds fresh."""
    ...
```

### Task G6: Property tests

**Files:**
- Create: `tests/properties/test_daemon_concurrency.py`
- Create: `tests/properties/test_daemon_spawn_race.py`

```python
# tests/properties/test_daemon_concurrency.py
from hypothesis import given, settings, strategies as st


@given(num_proxies=st.integers(min_value=2, max_value=10),
       num_captures_per=st.integers(min_value=1, max_value=5))
@settings(max_examples=20, deadline=None)
def test_concurrent_captures_all_land_exactly_once(num_proxies, num_captures_per):
    """N proxies × M captures each → N*M markdown files, no dupes, no losses."""
    ...


# tests/properties/test_daemon_spawn_race.py
@pytest.mark.asyncio
async def test_spawn_race_n_100(tmp_path):
    """100 simultaneous spawn attempts → exactly one daemon (closes deep-plan A5)."""
    ...
```

### Task G7: Dispatch isolation + embedding cache concurrency

**Files:**
- Create: `tests/daemon/test_dispatch_isolation.py`
- Create: `tests/daemon/test_embedding_cache_concurrency.py`

```python
# tests/daemon/test_dispatch_isolation.py
@pytest.mark.asyncio
async def test_two_proxies_distinct_request_ids_no_response_crosstalk(tmp_path):
    """Closes deep-plan M4 — guards against FastMCP version bump causing response cross-talk."""
    ...
```

```python
# tests/daemon/test_embedding_cache_concurrency.py
@pytest.mark.asyncio
async def test_50_concurrent_embeds_no_cache_corruption(tmp_path):
    """Closes deep-plan M1 — embedding cache writes are atomic + idempotent."""
    ...
```

### Task G8: 9 hermetic CLI smoke tests

**Files:**
- Modify: `tests/test_phase4_cli_smoke.py`

- [ ] **Step 1: Append 9 daemon-mode smoke tests**

Each spawns the actual binary via `subprocess.run` against `tmp_path` vaults and asserts observable state. Pattern matches existing smoke tests in this file. The 9 tests (from spec Section 14.5):

```python
# Append to tests/test_phase4_cli_smoke.py

def test_smoke_engram_serve_proxy_default_cold(tmp_path):
    """engram serve against cold vault: spawns daemon + connects + responds to list_tools."""
    vault = _prepare_smoke_vault(tmp_path)
    proc = subprocess.run(
        ["engram", "serve", "--config", str(vault / ".engram" / "config.yaml")],
        input=_initialize_then_list_tools_frames(),
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == 0
    # Daemon spawned + is still running (socket persists until idle-shutdown)
    assert (vault / ".indexes" / "engram.sock").exists() is True
    # Teardown: stop the daemon so the test fixture is clean
    subprocess.run(
        ["engram", "daemon", "stop", "--config", str(vault / ".engram" / "config.yaml")],
        timeout=15,
        check=True,
    )
    assert (vault / ".indexes" / "engram.sock").exists() is False


def test_smoke_engram_serve_no_daemon(tmp_path):
    """engram serve --no-daemon: today's path; no socket created."""
    ...


def test_smoke_engram_daemon_start_foreground(tmp_path):
    """engram daemon start blocks; SIGTERM cleans up."""
    ...


def test_smoke_engram_daemon_start_detach(tmp_path):
    """engram daemon start --detach: returns immediately; PID alive; socket present."""
    ...


def test_smoke_engram_daemon_stop(tmp_path):
    """After daemon start --detach, daemon stop kills it cleanly."""
    ...


def test_smoke_engram_daemon_status_text(tmp_path):
    """engram daemon status against running daemon: text output matches expected fields."""
    ...


def test_smoke_engram_daemon_status_json(tmp_path):
    """engram daemon status --json: valid JSON, required keys present."""
    ...


def test_smoke_engram_daemon_logs(tmp_path):
    """engram daemon logs --tail N: returns N lines."""
    ...


def test_smoke_engram_daemon_logs_follow(tmp_path):
    """engram daemon logs --follow: streams new log lines."""
    ...
```

### Layer G commit

- [ ] **Step 1: Stage + verify + commit**

```bash
git add tests/integration/test_daemon_*.py tests/daemon/test_crash_recovery.py \
        tests/daemon/test_dispatch_isolation.py tests/daemon/test_embedding_cache_concurrency.py \
        tests/properties/test_daemon_*.py tests/test_phase4_cli_smoke.py
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest -v
git commit -S -s -m "test(daemon): Layer G — integration + property + 9 hermetic CLI smoke tests

Coverage:
- 5 concurrent proxies (the core multi-session win)
- Fingerprint dedup with same-content captures
- Spawn race N=2 (integration) + N=100 (property test; closes A5)
- Idle shutdown timer + auto-wake (closes H3 race)
- SO_PEERCRED reject path
- Multivault preservation (daemon mounts primary + extras)
- --no-daemon regression (today's path bit-for-bit)
- Crash recovery: SIGKILL mid-capture; 3-retry exp backoff; replay idempotency
- Dispatch isolation across proxies (closes M4 FastMCP-bump risk)
- Embedding cache concurrent write (closes M1)
- 9 hermetic CLI smoke tests against installed binary

Spec: ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md Section 14"
```

**Approx LOC Layer G:** ~600 test code (no new source).

---

## Layer H — ADR + docs + CHANGELOG + cross-repo

**Goal:** Author ADR 008, the operator guide, update 9 engram docs, rotate CHANGELOG to v0.5.0, and handle cross-repo doc surface (idea-forge MANIFEST + spec renumbering + dotfiles note).

**Files this layer creates or modifies:**

**engram repo (`~/repos/github.com/kpachhai/engram/`):**
- Create: `docs/adr/008-daemon-mode.md`
- Create: `docs/DAEMON_MODE.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/QUICKSTART.md`
- Modify: `docs/USE_CASES.md`
- Modify: `docs/COMPARISONS.md`
- Modify: `docs/MULTI_MACHINE_SETUP.md`
- Modify: `docs/MULTI_VAULT_SETUP.md`
- Modify: `docs/DEPLOYMENT_MODEL.md`
- Modify: `CHANGELOG.md`

**idea-forge repo (`~/repos/github.com/kpachhai/idea-forge/`):**
- Modify: `workspace/engram/MANIFEST.md`
- Modify: `workspace/engram/PENDING_TASKS.md`
- Modify: `workspace/engram/skill-audit-log.md`
- Modify: `workspace/engram/PHASE_4_RETROSPECTIVE.md` (if it mentions Phase 5 enterprise — sweep)
- Modify: `docs/superpowers/specs/2026-05-04-engram/03-ROADMAP.md` (renumber + insert new Phase 5 daemon section)
- Modify: `docs/superpowers/specs/2026-05-04-engram/00-VISION.md` (Phase 5/6 renumber sweep)
- Modify: `docs/superpowers/specs/2026-05-04-engram/01-PRODUCT_SPEC.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/02-TECHNICAL_DESIGN.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/05-HANDOFF.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/06-SECURITY.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/08-COMPETITIVE_LANDSCAPE.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/09-MESH_BRAIN.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/11-IMPLEMENTATION_PROMPT.md`

**dotfiles repo (`~/repos/github.com/kpachhai/dotfiles/`):**
- Modify: `dot_claude/CLAUDE.md.tmpl` ("Multiple Persistent-Memory MCPs" — small note)

### Task H1: ADR 008

**Files:**
- Create: `~/repos/github.com/kpachhai/engram/docs/adr/008-daemon-mode.md`

- [ ] **Step 1: Author from the spec's Section 20 outline**

Use the outline in `~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md` Section 20. Sections: Context (link to Section 2 of spec), Decision (link to each design choice's spec section), Alternatives Considered, Consequences (positive/negative/neutral), Pinned-Invariant Analysis (link to spec Section 20.5 table), CLAUDE.md operational-line amendment.

Target length: ~250-400 lines. Match the format of ADRs 001-007 in `docs/adr/`.

- [ ] **Step 2: Verify file created + readable**

Run: `wc -l docs/adr/008-daemon-mode.md`
Expected: 250-400 lines.

### Task H2: DAEMON_MODE.md operator guide

**Files:**
- Create: `~/repos/github.com/kpachhai/engram/docs/DAEMON_MODE.md`

- [ ] **Step 1: Author**

Audience: engram operators. Sections:
1. Overview (what daemon mode does + why it exists)
2. Quick start (`engram serve` Just Works; no config change needed)
3. When the daemon spawns + when it shuts down
4. Inspecting the daemon (`engram daemon status` examples + JSON)
5. Stopping the daemon (`engram daemon stop` examples)
6. Tailing logs (`engram daemon logs` + `--follow` semantics)
7. Troubleshooting:
   - Stale socket: run `engram daemon start` to clean up.
   - Spawn timeout: check `daemon.spawn_timeout_seconds` + WAL recovery grace.
   - Peer-cred reject: indicates a non-self UID tried to connect — investigate.
   - `--no-daemon` mutual exclusion: stop the daemon first OR stop the other serve.
   - Long UDS path on macOS: symlink your vault into a shorter path.
8. Config knobs (full DaemonConfig field reference with defaults).
9. Downgrade path (stop daemon BEFORE installing v0.4.x; remove `daemon:` block).

Target length: ~400-600 lines.

- [ ] **Step 2: Verify**

Run: `wc -l docs/DAEMON_MODE.md`

### Task H3: Update existing engram docs

**Files:**
- Modify: `CLAUDE.md` (TWO edits: amend the "MCP server: stdio only" line PLUS amend the "Repository layout" section's spec back-reference — the spec moved INTO engram on 2026-05-12, so the back-reference paragraph saying *"The spec lives outside this repo at `~/repos/github.com/kpachhai/idea-forge/docs/superpowers/specs/2026-05-04-engram/`"* must be updated to *"The spec lives in this repo at `docs/superpowers/specs/2026-05-04-engram/` (gitignored — local working artifact, not part of the public release)."* The PII discipline carve-out for "spec back-reference exception" becomes moot since there's no cross-repo back-reference anymore — that bullet in CLAUDE.md PII section can simplify.)
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/QUICKSTART.md`
- Modify: `docs/USE_CASES.md`
- Modify: `docs/COMPARISONS.md`
- Modify: `docs/MULTI_MACHINE_SETUP.md`
- Modify: `docs/MULTI_VAULT_SETUP.md`
- Modify: `docs/DEPLOYMENT_MODEL.md`

- [ ] **Step 1: Apply edits per spec Section 19.1**

For each file, the spec lists the exact change. E.g., `CLAUDE.md`: amend the "MCP server: stdio only" operational line per the amendment in Section 21 of the spec. `README.md`: add "Multi-session support" subsection to features + new entries to "Common operations". `docs/ARCHITECTURE.md`: add daemon section with process diagram (reuse the spec's Section 4.1 ASCII diagram).

- [ ] **Step 2: PII Pre-Write Checklist applied to every file**

Per the maintainer's global CLAUDE.md "Pre-Write Checklist for Publishable Repos" — scan each file for: real names, emails, employer brand names, hardcoded `/Users/<name>/` paths, secrets. Engram is a publishable repo.

- [ ] **Step 3: Verify all edits applied**

```bash
grep -n "daemon" docs/ARCHITECTURE.md docs/QUICKSTART.md docs/USE_CASES.md docs/COMPARISONS.md docs/MULTI_MACHINE_SETUP.md docs/MULTI_VAULT_SETUP.md docs/DEPLOYMENT_MODEL.md README.md CLAUDE.md | wc -l
```
Expected: ≥ 20 matches across the 9 files.

### Task H4: CHANGELOG.md → v0.5.0

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Rotate `[Unreleased]` → `[0.5.0] - 2026-05-XX`**

Add the v0.5.0 entry with:
- Header: "Phase 5: Daemon Mode (multi-session support via per-vault UDS daemon)"
- Subsections: Added (new CLI subcommands, config fields, doctor checks), Changed (engram serve default behavior; CLAUDE.md operational-line amendment), Migration notes (downgrade procedure — closes M6), Spec reference, Phase renumbering note ("Old roadmap Phase 5/6 enterprise → renumbered to Phase 6/7").

Add a fresh `[Unreleased]` header above.

### Task H5: Cross-repo — idea-forge MANIFEST + PENDING_TASKS

**Note (post-2026-05-12 spec move):** The `docs/superpowers/specs/2026-05-04-engram/` directory moved INTO the engram repo (`docs/superpowers/` is gitignored there per engram's `.gitignore:64`). The spec renumber sweep is now an engram-internal local-only edit (Task H5a below), not a cross-repo commit. This cross-repo task is now smaller — just idea-forge planning-surface updates + a stale-path fix in MANIFEST.

**Files:** (in `~/repos/github.com/kpachhai/idea-forge/`)
- Modify: `workspace/engram/MANIFEST.md` (update phase table AND update the `Spec source:` field — it currently says `docs/superpowers/specs/2026-05-04-engram/ (in idea-forge)`; should now say `docs/superpowers/specs/2026-05-04-engram/ (in engram, gitignored)`)
- Modify: `workspace/engram/PENDING_TASKS.md`
- Modify: `workspace/engram/skill-audit-log.md`

- [ ] **Step 1: Update MANIFEST.md**

Add a new Phase 5 row (daemon mode); renumber old "5-6 Enterprise scaffolding / polish" → "6-7" with date-of-renumber note. Status snapshot updated.

- [ ] **Step 2: Update PENDING_TASKS.md**

Add Phase 5 operational dogfood criterion:

```markdown
### Phase 5 — Daemon mode operational dogfood

- [ ] Maintainer runs 2 concurrent Claude Code sessions against memex personal vault for ≥7 consecutive days. Daily checks:
  - `engram daemon status` reports expected proxy count + uptime + error counter
  - No orphaned sockets after laptop sleep/wake cycles
  - No fallback to `--no-daemon` or per-session vaults required
```

- [ ] **Step 3 was: spec renumber sweep — MOVED to new Task H5a (engram-local edit)**

The spec renumber sweep originally lived here as a cross-repo task. Post-2026-05-12, the spec directory is inside the engram repo (under `docs/superpowers/`, gitignored), so the renumber is engram-local. See Task H5a below.

### Task H5a (NEW — post spec move): Engram-local spec renumber sweep

**Files:** (in `~/repos/github.com/kpachhai/engram/`, all under `docs/superpowers/specs/2026-05-04-engram/` — local-only since `docs/superpowers/` is gitignored)
- Modify: `docs/superpowers/specs/2026-05-04-engram/03-ROADMAP.md` (primary edit + new Phase 5 section)
- Modify: `docs/superpowers/specs/2026-05-04-engram/00-VISION.md` (renumber sweep)
- Modify: `docs/superpowers/specs/2026-05-04-engram/01-PRODUCT_SPEC.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/02-TECHNICAL_DESIGN.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/05-HANDOFF.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/06-SECURITY.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/08-COMPETITIVE_LANDSCAPE.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/09-MESH_BRAIN.md`
- Modify: `docs/superpowers/specs/2026-05-04-engram/11-IMPLEMENTATION_PROMPT.md`

- [ ] **Step 1: Edit `docs/superpowers/specs/2026-05-04-engram/03-ROADMAP.md`**
  - Insert new `## Phase 5 - Daemon Mode (Multi-Session Support)` section before old Phase 5 section. Body: brief summary linking to the design spec; decision gates ("ships with v0.5.0"); dependencies ("Phase 4 complete"); non-goals (echo from spec Section 3).
  - Rename old `## Phase 5 - Enterprise Scaffolding` → `## Phase 6 - Enterprise Scaffolding`.
  - Rename old `## Phase 6 - Enterprise Polish` → `## Phase 7 - Enterprise Polish`.
  - Update "Decision Gates - Summary Table" + "Dependencies and Sequencing" sections.

- [ ] **Step 2: Sweep the other 8 spec files for "Phase 5" / "Phase 6" references**

```bash
cd ~/repos/github.com/kpachhai/engram
for f in docs/superpowers/specs/2026-05-04-engram/{00,01,02,05,06,08,09,11}-*.md; do
  grep -n "Phase [56]" "$f"
done
```
Then edit each file via Edit tool, applying the bump rule from spec Section 18.2 (old Phase 5 enterprise → Phase 6; old Phase 6 polish → Phase 7; references to "Phase 1-4" stay as-is).

- [ ] **Step 3: No commit needed**

`docs/superpowers/` is in engram's `.gitignore`. These edits are local-only working-tree changes for the maintainer's planning surface. `git status` after editing should show nothing in `docs/superpowers/`.

### Task H6: Cross-repo — dotfiles note

**Files:**
- Modify: `~/repos/github.com/kpachhai/dotfiles/dot_claude/CLAUDE.md.tmpl`

- [ ] **Step 1: Add a brief note in "Multiple Persistent-Memory MCPs" section**

Add (after the existing rule body):

```
> **Update 2026-05-XX (engram v0.5.0):** engram's prior single-session limit was resolved via daemon mode (Phase 5). The workaround chain ("per-session vaults / --force takeover / OB-only fallback") is no longer needed. Multiple Claude Code sessions can attach engram-MCP to the same vault simultaneously. The capability-conditional MCP rule itself is unchanged.
```

The chezmoi template doesn't need conditional logic — engram v0.5.0+ is the same behavior across all machines.

### Task H7: Capture [Resolution] event

**Files:**
- Modify: `~/.claude/friction-log.md`

- [ ] **Step 1: Append a [Resolution] row**

```bash
printf '%s | [Resolution] | %s | supersedes: %s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  'engram daemon mode shipped in v0.5.0; N concurrent Claude sessions can now attach engram-MCP to the same vault. Workaround chain (per-session vaults / --force takeover / OB-only fallback) retired.' \
  '2026-05-12T14:20:04Z engram stdio + fcntl.flock = 1 Claude session per vault.' \
  >> ~/.claude/friction-log.md
```

- [ ] **Step 2: Capture to Open Brain + engram**

```python
# Run from a Claude session or via the engram CLI
mcp__open-brain__capture_thought(content="[Resolution] engram v0.5.0 daemon mode shipped 2026-05-XX. Multiple Claude Code sessions now attach to the same engram vault via per-vault UDS daemon. Closes the 2026-05-12 [Friction]. Implementation: 8 layers (A-H), ~1100-1300 LOC, ~80-100 new tests. PHASE_5_PLAN.md in the engram repo documents the full design. Operational dogfood: 7-day two-session window in memex vault.")
mcp__engram__capture_thought(content="<same content>")
```

### Layer H commit (engram repo only — cross-repo commits land separately)

- [ ] **Step 1: Stage engram repo files + verify + commit**

```bash
cd ~/repos/github.com/kpachhai/engram
git add docs/adr/008-daemon-mode.md docs/DAEMON_MODE.md \
        CLAUDE.md README.md docs/ARCHITECTURE.md docs/QUICKSTART.md \
        docs/USE_CASES.md docs/COMPARISONS.md docs/MULTI_MACHINE_SETUP.md \
        docs/MULTI_VAULT_SETUP.md docs/DEPLOYMENT_MODEL.md CHANGELOG.md \
        docs/PHASE_5_PLAN.md docs/PHASE_5_LAYER_A_AUDIT.md
git commit -S -s -m "docs(daemon): Layer H — ADR 008 + DAEMON_MODE guide + 9 doc updates + CHANGELOG v0.5.0

- ADR 008 captures the per-vault topology + auto-spawn + UDS rationale
  + alternatives (HTTP loopback, per-user daemon, hybrid)
- DAEMON_MODE.md operator guide
- CLAUDE.md amends 'MCP server: stdio only' to clarify UDS is local IPC
- README + ARCHITECTURE + QUICKSTART + USE_CASES + COMPARISONS +
  MULTI_MACHINE_SETUP + MULTI_VAULT_SETUP + DEPLOYMENT_MODEL updated
- CHANGELOG rotates [Unreleased] -> [0.5.0] with downgrade-procedure
  note (closes M6)
- PII Pre-Write Checklist applied to all 11 files

Spec: ~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md"
```

### Cross-repo commits (idea-forge + dotfiles — separate commits per repo's own conventions)

- [ ] **Step 1: idea-forge commit (post-spec-move: smaller scope, planning surface only)**

```bash
cd ~/repos/github.com/kpachhai/idea-forge
git add workspace/engram/MANIFEST.md workspace/engram/PENDING_TASKS.md \
        workspace/engram/skill-audit-log.md
git commit -S -s -m "docs(engram): Phase 5 (daemon mode) planning surface updates

- workspace/engram/MANIFEST: Phase 5 daemon row added; 5-6 enterprise renumbered
  to 6-7; Spec source field updated to point at engram (specs moved on 2026-05-12)
- workspace/engram/PENDING_TASKS: Phase 5 operational dogfood criterion added
- workspace/engram/skill-audit-log: any python-package-builder lessons surfaced
  during Phase 5 implementation

Note: the canonical design spec
(docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md) +
docs/superpowers/specs/2026-05-04-engram/ original spec live INSIDE the engram
repo under docs/superpowers/ which is gitignored — local working artifacts."
```

- [ ] **Step 2: dotfiles commit**

```bash
cd ~/repos/github.com/kpachhai/dotfiles
git add dot_claude/CLAUDE.md.tmpl
git commit -S -s -m "docs(claude-md): engram v0.5.0 resolves single-session friction

Note appended to 'Multiple Persistent-Memory MCPs' section: the
workaround chain (per-session vaults / --force / OB-only fallback)
is no longer needed since engram daemon mode supports N concurrent
Claude sessions attaching to one vault."
```

**Approx LOC Layer H:** ~750-800 doc (ADR 008 + DAEMON_MODE.md + 9 update sweeps + CHANGELOG + cross-repo).

---

## Phase Exit Gate

Per the engram CLAUDE.md "Code Project Completion Gate" — three blockers (not warnings):

### 1. `verify-before-done` checklist

- [ ] **Stderr discipline**: every test run checked for warnings; uv build + ruff + mypy clean.
- [ ] **Bounds checks**: `connected_proxies ≥ 0`; `uptime_seconds ≥ 0`; idle-timer state transitions never negative.
- [ ] **Edge cases**: empty vault, corrupt state file, partial spawn (daemon dies mid-startup), socket-already-bound at startup, SIGTERM during drain, idle timer race with new connect — all covered by Layer G integration tests.
- [ ] **Regression tests**: fail-pre / pass-post for each bug discovered during Layer G.
- [ ] **Scope honesty**: commit-message scope matches `git diff --stat`; no inflated claims.
- [ ] **Explicit list of NOT-verified surfaces**: structured per CLAUDE.md "list what was NOT verified" rule. Categories:
  - **handler-tests-only items**: unit tests cover the per-module behavior but not the cross-process integration with the actual fastmcp version pin.
  - **no-multi-machine-test**: daemon mode tested on a single machine; cross-machine vault scenarios (L2) covered only by code-level state-file-hostname check.
  - **no-7-day-dogfood**: operational criterion deferred to live deployment (Phase 5 Op #1).
  - **no-launchd/systemd-test**: V1.1 / Phase 5.5 deferred.
  - **no-load-test**: 100+ concurrent proxies tested by property test but not in a sustained-load scenario.
- [ ] **Section 6 spec audit** (public-release-shipping milestone): sub-agent walks `~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md` and cross-checks against `src/engram/daemon/` + `src/engram/cli/daemon.py`. Classifications: IMPLEMENTED / PARTIAL / MISSING / DEFERRED. The audit is itself a commit (`docs/PHASE_5_SPEC_AUDIT.md`).

### 2. `comprehension-gate` Step 5 — 4-question artifact

Maintainer authors the four-question artifact:

1. **Why was per-vault daemon topology chosen?** (vs per-user multi-vault daemon)
2. **Why was UDS chosen?** (vs HTTP-on-loopback)
3. **Where does state live?** (markdown SoT, SQLite cache, daemon in-memory connected-count + last-request-at + counters)
4. **What's the failure mode?** (daemon crash → proxy 3-retry exp backoff → MCP error to Claude; idle shutdown → auto-wake on next session)

Committed alongside the Phase 5 retrospective in `~/repos/github.com/kpachhai/idea-forge/workspace/engram/PHASE_5_RETROSPECTIVE.md`.

### 3. Hermetic CLI smoke

- [ ] All 9 new smoke tests in `tests/test_phase4_cli_smoke.py` PASS against the installed binary (`pip install -e .` first).
- [ ] All 15 existing smoke tests still PASS (no regression).
- [ ] `uv run pytest --cov=src --cov-fail-under=80` still passes (coverage gate).

### 4. Operational criterion (deferred)

- [ ] **Phase 5 Op #1**: Maintainer runs 2 concurrent Claude Code sessions against memex for ≥7 days. Logged in `workspace/engram/PENDING_TASKS.md`.

---

## Risks + Mitigations Summary

Aggregated from spec Section 22 + deep-plan sub-agent findings + this plan's amendment delta. Each risk maps to a layer/test that addresses it.

| Risk | Severity | Closed by |
|---|---|---|
| H1: Spawn lock holder dies between unlink + bind | HIGH | Amendment 1 (acquire VaultLock BEFORE bind); Layer C step 3 ordering; Layer G test_spawn_race_10_simultaneous_invocations |
| H2: SyncCoordinator mid-push during SIGTERM loses commits | HIGH | Amendment 2 (coordinator-drain timeout distinct from outer stop); Layer C step 3 graceful drain |
| H3: Idle shutdown vs new-proxy-connect race | HIGH | Amendment 3 (two-phase atomic shutdown); Layer G test_idle_timer_does_not_fire_with_connected_proxies |
| H4: WAL recovery vs spawn timeout | HIGH | Amendment 4 (`wal_recovery_grace_seconds`); Layer D `_spawn_daemon_process` effective-timeout calculation |
| H5: pytest macOS UDS path-length collision | HIGH | Amendment 5 (104-byte check); Layer A test_resolve_paths_rejects_long_uds_path; Layer E daemon_socket_path_too_long doctor check |
| M1: Embedding cache concurrent-writer corruption | MEDIUM | Layer G test_embedding_cache_concurrency |
| M2: PII in `engram daemon logs` | MEDIUM | Amendment 9 (log redaction default + DEBUG banner); Layer F `engram daemon logs` warning |
| M3: --no-daemon vs daemon mutual-exclusion UX | MEDIUM | Layer F serve_cmd error path with remediation hint |
| M4: FastMCP version pin drift | MEDIUM | Layer G test_dispatch_isolation + uv.lock pin commitment |
| M5: Signal-handler stacking | MEDIUM | Amendment 1 (signal handlers BEFORE resources); Layer C _install_signal_handlers ordering |
| M6: Downgrade with running daemon | MEDIUM | Amendment 10 (CHANGELOG migration note) |
| L1: TOCTOU on socket path | LOW | Out of scope per threat model; documented in spec Section 7.3 |
| L2: Cross-machine vault path resolution | LOW | Amendment 1 + state file hostname; Layer E doctor check on hostname mismatch (followup if needed) |
| L3: Proxy connects but never sends | LOW | Amendment 4 (`connection_idle_timeout_seconds`); Layer C handle_connection asyncio.wait_for timeout |

---

## References

- **Design spec:** `~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md`
- **Engram CLAUDE.md** (conventions, pinned invariants): `~/repos/github.com/kpachhai/engram/CLAUDE.md`
- **Original engram spec** (Phase 1-4 + roadmap): `~/repos/github.com/kpachhai/engram/docs/superpowers/specs/2026-05-04-engram/`
- **Python-package-builder skill** (cadence, Phase Exit discipline): `~/repos/github.com/kpachhai/idea-forge/skills/code-projects/python-package-builder/SKILL.md`
- **Code-project foundation** (8-layer model, exit criteria split): `~/repos/github.com/kpachhai/idea-forge/skills/code-projects/code-project-foundation/SKILL.md`
- **Code-project-retrospective skill** (post-Phase artifacts): `~/repos/github.com/kpachhai/idea-forge/skills/code-projects/code-project-retrospective/SKILL.md`
- **Existing engram lock implementation** (single-writer rationale): `~/repos/github.com/kpachhai/engram/src/engram/utils/lock.py`
- **Existing engram serve flow** (the path Phase 5 wraps): `~/repos/github.com/kpachhai/engram/src/engram/cli/serve.py`

---

**End of Phase 5 plan.**

**Total estimated cost:**
- Source: ~1200 LOC (Layers A-F, ~1100 LOC from spec + amendments adding ~80 LOC for new config fields, `connect_during_drain` counter, hostname in state, max_frame_bytes wiring)
- Tests: ~900 LOC (Layer G ~600 + per-layer unit tests ~300)
- Docs: ~800 LOC (Layer H ADR + DAEMON_MODE + 9 updates + CHANGELOG + cross-repo)
- Net: ~2900 LOC total across ~25-30 commits (8 layer commits + per-layer fix-up commits + cross-repo commits)

**Estimated implementation time:** 5-8 working days for the maintainer at engram's prior pace (Phase 4 was 8 layers + similar scope; this is comparable).
