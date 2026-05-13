# Phase 5 — Layer A pre-implementation audits

Captured: 2026-05-13 (Layer A task A6)

Spec source: `docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md` (gitignored under `docs/superpowers/`).
Companion: `docs/PHASE_5_FASTMCP_AUDIT.md` (Task A0.5).

## Audit 1 — `VaultStorage` SQLite connection model

**Question:** Does the existing `VaultStorage` use one shared SQLite connection across handlers, or per-call connections? This determines whether daemon mode needs an additional `asyncio.Lock` around writes.

**Evidence (read directly from the repo):**

- `src/engram/storage/facade.py:116` — `class VaultStorage` is constructed once and holds `self.conn: sqlite3.Connection` (line 151) opened via `engram.storage.sqlite.open_connection(...)`. The connection is owned for the lifetime of the `VaultStorage` instance.
- `src/engram/storage/sqlite.py:120` — `open_connection(...)` opens the SQLite file in WAL mode (`PRAGMA journal_mode=WAL`) with `isolation_level=None` (autocommit). sqlite-vec is loaded on the same connection.
- `src/engram/storage/facade.py:327,384,399,416,452,486,509,524,555,561,569` — every storage query path (read AND write) uses the single `self.conn` reference. No code path opens a second connection.

**Answer:** ONE shared connection per `VaultStorage` instance. WAL mode + sqlite-vec loaded once.

**Implication for daemon mode:** the daemon holds the only `VaultLock` for its primary vault and owns the only `VaultStorage` instance for that vault. Concurrent per-connection asyncio tasks all dispatch through this single storage. SQLite WAL's at-most-one-writer guarantee, combined with the single-storage / single-connection model and Python's GIL plus asyncio's cooperative scheduling, means writes naturally serialize at the SQLite engine level. **No additional `asyncio.Lock` is required around the write path for Phase 5** — Phase 4's storage facade is daemon-mode-safe as-is.

If property test G2 (concurrent captures from N proxies) ever fails due to race symptoms, revisit this audit and add an `asyncio.Lock` around `VaultStorage.capture` and friends. The cost would be ~5 LOC.

## Audit 2 — FastMCP per-connection dispatch entrypoint

**Question:** Does FastMCP expose a per-request dispatch entrypoint, or only the all-or-nothing `server.run()` stdio loop?

**Answer:** Public surface is only `FastMCP.run()` / `run_async()` / `run_stdio_async()` — all loop-owning. The full audit lives in **`docs/PHASE_5_FASTMCP_AUDIT.md`** alongside this file.

**Summary (full detail in the FastMCP audit doc):**

- Pinned version: **fastmcp 3.2.4** (also the current latest 3.x as of 2026-05-13 — zero drift today).
- Chosen entrypoint: `FastMCP._mcp_server.run(read_stream, write_stream, init_options, raise_exceptions=False, stateless=False)` — the upstream `mcp.server.lowlevel.server.Server.run` contract.
- Confidence: **MEDIUM** (underscore-prefixed internal access).
- Mitigation: Layer C adds `src/engram/daemon/_fastmcp_compat.py` as a shim with a single concrete entrypoint resolver; Layer G adds `tests/daemon/test_dispatch_isolation.py` (Amendment 11) as the canary against future fastmcp signature changes.

**Implication for daemon mode:** Layer C step 1 picks **Option A** (use FastMCP's `_mcp_server.run` via the compat shim, with anyio in-memory streams per accepted UDS connection). Option B (build our own JSON-RPC parse/dispatch/serialize loop) is the documented fallback in Layer C step 1's module docstring; it activates only if a future fastmcp release breaks the shim AND the dispatch-isolation test cannot be repaired.

## Audit 3 — Baseline test count

Recorded in **`docs/PHASE_5_BASELINE.md`** (Task A0.7): 1165 collected, 15 smoke, 80% coverage. Phase 5 acceptance targets ≥1245 collected, ≥24 smoke, ≥80% coverage. The 1 pre-existing FastEmbed network-touching test failure is not in Phase 5's regression budget.
