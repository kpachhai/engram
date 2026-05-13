# ADR 008 - Daemon Mode (per-vault UDS daemon + auto-spawning proxy)

**Status**: Accepted
**Date**: 2026-05-13
**Phase**: 5
**Supersedes**: none
**Superseded-by**: none

## Context

Phases 1-4 made engram a personal, multi-machine, optionally
team-shared memory backend exposed to MCP clients through a single
``engram serve`` process per vault. The process model is one stdio
MCP server holding the vault's ``fcntl.flock`` advisory lock for its
lifetime. This is correct for one Claude Code session per vault. It
is wrong for the increasingly common case where a single user opens
two or more Claude Code sessions against the same memex personal
vault: the second session's ``engram serve`` invocation fails with
``LockError`` and the user is forced into ugly workarounds (per-session
vaults, ``--force`` lock takeover that drops the first session, or
falling back to an external memory service).

The friction was captured on 2026-05-12 and resolved by introducing
a per-vault daemon process. ``engram serve`` becomes a thin proxy
that auto-spawns the daemon on first invocation and attaches as one
of N concurrent clients. The MCP wire format observed by Claude Code
is unchanged, so existing MCP configurations need no edits. The
implementation lives in ``src/engram/daemon/`` and
``src/engram/cli/daemon.py`` (new typer
subcommand group).

Three constraints shape the design:

1. **Pinned invariant 6 (MCP wire format stability).** Every byte the
   proxy emits to Claude Code must be the byte the daemon's FastMCP
   handler produced. The daemon does not invent new MCP semantics;
   it is the same FastMCP server running behind a UDS multiplexer.
2. **Pinned invariant 4 (two-layer enforcement at security
   boundaries).** Same-UID enforcement combines UDS filesystem perms
   (0o600 + owner-only directory) with ``SO_PEERCRED`` / ``getpeereid``
   on every accepted connection. Either alone is weaker than both.
3. **Pinned invariant 1 (per-thought portability gate).** The
   portability classification is enforced at the storage facade
   layer, which the daemon owns. Adding the daemon does not weaken
   the gate; it actually concentrates the enforcement point into a
   single long-lived process.

## Decisions

### D1. Per-vault daemon topology

One daemon process per vault. The daemon owns the ``VaultLock``, the
``VaultStorage`` (with the single shared SQLite connection per
Layer-A audit), the ``SyncCoordinator``, the ``FastEmbedProvider``,
and the built FastMCP server. The daemon listens on a per-vault
Unix Domain Socket at ``<vault>/.indexes/engram.sock``.

**Why per-vault rather than per-user.** Engram already has
multi-vault primitives from Phase 3 (one primary + N read-only
extras). The daemon for a primary mounts the extras read-only via
the unchanged ``_build_multivault_server_for`` path, so cross-vault
aggregation continues to work in-process. A per-user daemon would
have required redesigning multi-vault mount semantics. Per-vault
daemon falls out of the existing architecture.

### D2. Auto-spawn on first ``engram serve``

The proxy half of ``engram serve`` tries ``connect()`` to the UDS
first. On miss it acquires a per-vault spawn flock, ``fork`` + ``execvpe``
into ``engram daemon start --vault-path <path> --readiness-fd <fd>``,
waits for ``ready\n`` on the readiness pipe, then attaches. The MCP
config that exists today (``engram serve``) Just Works after upgrade
to v0.5.0.

**Why auto-spawn rather than requiring an explicit service.** The
existing operator UX is "open Claude Code, engram tools appear".
Requiring a separate ``engram daemon start`` step before opening
Claude breaks that. A separate v1.1 enhancement may add a
``launchd``/``systemd-user`` service generator for power users who
want zero first-session latency, but auto-spawn is the default.

### D3. UDS over HTTP-on-loopback

The wire is ``AF_UNIX`` ``SOCK_STREAM`` with newline-delimited
JSON-RPC framing. No TCP, no TLS, no Authorization header.

**Why UDS.** Filesystem permissions (0o600) plus
``SO_PEERCRED`` / ``getpeereid`` give a same-UID security model with
no extra moving parts. HTTP-on-loopback would need a port allocation
scheme, an auth-token rotation story (you can't trust ``127.0.0.1``
on multi-user hosts), and a TLS-or-not decision. UDS sidesteps every
one of those questions.

### D4. Daemon startup ordering contract

The forked daemon child runs an exact 12-step startup dance:
install signal handlers, acquire ``VaultLock``, run probes, detect
cloud-sync paths, open ``VaultStorage`` + build coordinator + build
FastMCP server, unlink any stale socket file, bind, ``chmod 0o600``,
write ``engram.state.json`` with PID + hostname, write ``ready\n``
to the readiness pipe, enter the accept loop.

**Step 0 (process isolation, before step 1):** ``_attach_daemon_log_handler``
calls ``os.setsid()`` to place the daemon in a new session and
process group, then redirects fd 1 and fd 2 to ``/dev/null``. This
ensures (a) SIGTERM sent to the spawning proxy's PGID (e.g. on Claude
Code session close) does not propagate to the daemon, and (b) writes
to the inherited stdout fd do not trigger ``BrokenPipeError`` in the
rich console handler once the proxy exits. Both of these caused the
daemon to die on every Claude Code restart before this fix.

**Why this order.** Signal handlers before resources: SIGTERM
during init still cleans up. ``VaultLock`` before bind: two racing
spawners cannot both pass step 2. Unlink before bind: stale-socket
recovery is part of the spawn. ``state.json`` after bind: status
readers can trust the file when it exists. ``ready`` last: the
proxy attaches only when everything is ready.

### D5. Graceful shutdown drain + two-phase idle shutdown

Shutdown drains in a fixed order with explicit budgets: close
listener, wait for in-flight tasks (``shutdown_drain_seconds``),
``coordinator.force_flush`` (``coordinator_flush_seconds`` — DISTINCT
from the outer ``engram daemon stop`` 60s wait so long git pushes
do not race the CLI timeout), close storage, release vault lock,
unlink socket + state file. Idle shutdown uses a two-phase atomic
pattern: ``asyncio.Lock`` guards the re-check of connected-proxies
and the listener close so reconnects during the countdown are
never silently dropped.

### D6. FastMCP per-connection dispatch via ``_mcp_server.run``

FastMCP exposes only loop-owning entrypoints (``run_stdio_async`` and
friends). For per-connection dispatch the daemon reaches into
``FastMCP._mcp_server`` (the upstream ``mcp.server.lowlevel.server.Server``)
and drives it with anyio in-memory streams per accepted UDS
connection. All access to ``_mcp_server`` goes through one shim file
(``src/engram/daemon/fastmcp_dispatch.py``); a contract smoke
(``tests/daemon/test_dispatch_isolation.py``) fails loudly if a
future fastmcp release moves or renames the attribute.

The contract is **upstream-MCP-canonical, not fastmcp-bespoke** — the
stream + ``SessionMessage`` + ``LowLevelServer.run`` pattern is owned
by the upstream ``mcp`` SDK, much more stable than fastmcp-side
renames. The historical FastMCP audit (archived under
``docs/archive/phases/``) rates confidence as MEDIUM for this
reason; the compat shim + smoke test bound the blast radius.

## Alternatives considered

### A1. Per-user single daemon mounting all vaults

Rejected. Phase 3 multi-vault already gives the per-vault daemon a
clean way to mount extras read-only. A per-user daemon would have
needed cross-vault routing rewiring + cross-vault permission rules.
Larger blast radius for no compelling benefit.

### A2. HTTP server on a per-vault loopback port

Rejected. Adds port allocation, auth-token rotation, optional TLS,
and CORS-shaped questions. UDS gives a smaller threat surface and
strictly stronger same-UID guarantees with no port hygiene story.

### A3. Hybrid: keep the existing ``--no-daemon`` path AND offer
daemon mode behind an opt-in flag

Rejected as default. The whole point of this work is to make N
concurrent sessions Just Work for the maintainer's own dogfood.
Opt-in daemon mode would have left the friction in place for the
common path. ``--no-daemon`` is preserved as an escape hatch for
embedded use cases + debugging.

### A4. Reuse FastMCP's ``run_stdio_async`` via stdin-stdout pipe pairs

Rejected. ``run_stdio_async`` reads from ``sys.stdin`` and writes to
``sys.stdout`` — it owns the process's stdio. Multiplexing N
connections through one FastMCP would have required forging file
descriptors per connection and was strictly more invasive than
using the documented ``LowLevelServer.run`` contract.

## Consequences

### Positive

- N concurrent Claude Code sessions can attach to the same vault
  simultaneously. The friction that motivated this work is closed.
- The MCP wire format is unchanged. No client config edits needed.
- Idle daemon auto-shuts down after configurable timeout (default
  30 seconds) so unused vaults don't keep a Python process alive
  after the last session closes.
- New CLI surface (``engram daemon start/stop/status/logs``) gives
  operators direct introspection on the running daemon.
- Six new doctor checks surface daemon-mode health.

### Negative

- First session after a cold start has ~1-2s extra latency
  dominated by ``FastEmbed`` model load (the daemon spawn dance).
  Subsequent concurrent sessions attach instantly.
- One more process in the user's process tree per active vault.
  ``ps`` and tooling that count engram processes need awareness.
- ``--no-daemon`` and daemon mode are mutually exclusive per
  ``VaultLock`` — operators who want to run both must stop the
  daemon first.
- The compat shim (``daemon/fastmcp_dispatch.py``) touches an
  underscore-prefixed fastmcp attribute. A future fastmcp release
  may move it; the shim plus contract smoke bound the blast
  radius but a fix is required when that happens.

### Neutral

- ``engram serve --no-daemon`` keeps today's behavior bit-for-bit
  for one-shot embedded use cases.
- Per-vault state lives at ``<vault>/.indexes/engram.{sock,spawn.lock,state.json,log}``;
  the existing ``engram.lock`` is co-located in the same directory.

## Pinned-invariant analysis

| # | Invariant | Status after daemon mode |
|---|---|---|
| 1 | Per-thought portability gate at the storage facade | Preserved — the daemon owns the single ``VaultStorage`` for the vault; the gate fires there as before. |
| 2 | Markdown SoT is canonical | Preserved — daemon writes to the same ``thoughts/<prefix>/...`` tree. |
| 3 | One writer per vault | Preserved AND strengthened — ``VaultLock`` is now held by a long-lived daemon, not a short-lived ``serve`` per session. |
| 4 | Two-layer enforcement at security boundaries | Preserved AND extended — UDS filesystem perms (0o600 + owner dir) combine with ``SO_PEERCRED`` / ``getpeereid`` per connection. |
| 5 | Sync coordinator drains on shutdown | Preserved — the drain contract is explicit with its own budget (``coordinator_flush_seconds``). |
| 6 | MCP wire format stable for v1.x | Preserved — daemon dispatches via the upstream MCP ``LowLevelServer`` so the bytes Claude observes are identical to v0.4.x output. |
| 7 | At-most-one-primary per user | Preserved — the daemon for a primary vault mounts read-only extras via the unchanged Phase 3 path. |

## CLAUDE.md operational-line amendment

The pre-Phase-5 ``CLAUDE.md`` says **"MCP server: stdio only"**. This
remains true at the proxy boundary (Claude Code talks to ``engram serve``
via stdio); it is augmented internally by a per-vault UDS between
proxy and daemon. The amendment in ``CLAUDE.md`` clarifies that the
UDS is local IPC, not a network listener, and is not part of the
client-facing MCP surface.
