# engram Daemon Mode

Daemon mode lets **N concurrent Claude Code sessions** attach to the
same engram vault simultaneously. It is the default in v0.5.0 and
later — your existing MCP config Just Works.

This guide is for engram operators: what daemon mode does, how to
inspect it, how to control it, and how to upgrade from a pre-v0.5.0
install.

---

## Quick start

After upgrading to v0.5.0, **no MCP config edits are required**. The
binary ``engram serve`` that your MCP config already points at now
runs in proxy mode by default: it auto-spawns a per-vault daemon on
first invocation and attaches as one of N concurrent clients.

The first session after a cold start has a small (~1-2s) spawn
latency dominated by FastEmbed's model load inside the daemon
process. Subsequent concurrent sessions attach in tens of
milliseconds because the daemon is already warm.

```text
Session 1 (cold): Claude Code -> engram serve (proxy) -> spawn daemon (~1s)
                                                        |
Session 2 (warm): Claude Code -> engram serve (proxy) - + (instant attach)
                                                        |
Session 3 (warm): Claude Code -> engram serve (proxy) -+
```

---

## Upgrading from pre-v0.5.0 (migration guide)

If you have engram installed today (any version <0.5.0) and an MCP
client (Claude Code, etc.) configured to invoke ``engram serve``,
here is the upgrade procedure:

### Step 1 — stop any running engram processes

Before installing v0.5.0, stop every ``engram serve`` process for
every vault. In a pre-v0.5.0 install there is no daemon yet, so this
just means quitting any Claude Code sessions (or any other MCP
client) that have engram tools active.

If you are using the dev-install pattern (``uv tool install --editable .``
against a working tree), there is no system service to stop. If you
distributed engram via a launchd / systemd-user unit, stop the unit.

### Step 2 — upgrade the binary

```bash
# In your engram checkout:
git pull
uv sync
uv tool install --editable . --reinstall

# Or from PyPI (when published):
pip install --upgrade engram-mcp
```

Verify the version:

```bash
engram --version    # should report 0.5.0 or later
engram daemon --help  # should list start/stop/status/logs subcommands
```

### Step 3 — verify your MCP config

Your existing MCP config should look something like:

```json
{
  "mcpServers": {
    "engram": {
      "command": "engram",
      "args": ["serve"]
    }
  }
}
```

**No changes required.** In v0.5.0 the same command runs in proxy
mode by default. Open Claude Code, send a tool call, and engram
will auto-spawn a daemon for your primary vault.

### Step 4 — verify the daemon spawns

After opening a Claude Code session and exercising an engram tool
once, run from a terminal:

```bash
engram daemon status
```

You should see something like:

```text
vault     : memex (~/.local/share/engram/memex)
daemon pid: 47832
started at: 2026-05-13T17:04:21+00:00
uptime    : 134s
socket    : ~/.local/share/engram/memex/.indexes/engram.sock
log file  : ~/.local/share/engram/memex/.indexes/engram.log
```

JSON form for scripting:

```bash
engram daemon status --json | jq .daemon.running
# -> true
```

### Step 5 — confirm multi-session works

Open a second Claude Code session against the same project. Inside,
exercise an engram tool. Back in the terminal:

```bash
engram daemon status --json | jq .socket.connected_proxies
```

Both Claude sessions are now attached to the one daemon process; no
``LockError``, no ``--force`` takeover.

### Optional: stop the daemon explicitly

When you close all Claude sessions, the daemon idles for 60 minutes
(default) then auto-shuts-down. If you want to stop it sooner:

```bash
engram daemon stop
# -> daemon stopped (pid 47832)
```

### Downgrade note (back to pre-v0.5.0)

If you ever need to downgrade to v0.4.x:

1. Run ``engram daemon stop`` first. v0.4.x does not know about
   daemon mode and ``engram serve`` will fail with ``LockError``
   until the v0.5.0 daemon is gone.
2. Remove any ``daemon:`` block from ``engram.config.yaml`` — v0.4.x's
   Pydantic model is ``extra="forbid"`` and will refuse the file
   with the block present.

---

## When the daemon spawns and when it shuts down

The daemon is **demand-spawned**:

- The first ``engram serve`` invocation against a cold vault forks
  the daemon and proxies as one of its clients.
- Subsequent ``engram serve`` invocations against the same vault
  detect the live socket and attach as additional proxies.

The daemon shuts down when:

- The idle timer fires (``daemon.idle_shutdown_seconds`` in
  ``engram.config.yaml``, default 3600 = 60 min). The timer counts
  down only while zero proxies are connected.
- An operator runs ``engram daemon stop`` (SIGTERM + bounded wait;
  ``--force`` SIGKILLs after the timeout).
- The host shuts down.

Auto-wake: after an idle shutdown, the next ``engram serve``
invocation spawns a fresh daemon. The transition is invisible to
Claude Code; same first-session latency cost.

---

## ``engram daemon status``

Default human-readable output:

```text
vault     : memex (~/.local/share/engram/memex)
daemon pid: 47832
started at: 2026-05-12T14:20:04+00:00
uptime    : 5027s
socket    : ~/.local/share/engram/memex/.indexes/engram.sock
log file  : ~/.local/share/engram/memex/.indexes/engram.log
```

When the daemon is not running, the command exits 0 (not-running is
a normal state) and prints:

```text
vault     : memex (~/.local/share/engram/memex)
daemon    : not running
socket    : not present at <path>
state file: not present at <path>
hint      : run `engram serve` (auto-spawn) or `engram daemon start --vault memex`
```

JSON form for scripting:

```bash
engram daemon status --json
```

Returns the full payload — ``daemon.running`` is the key consumers
should branch on.

---

## ``engram daemon stop``

Graceful stop:

```bash
engram daemon stop
```

SIGTERMs the daemon, waits up to 60s for the coordinator to flush
its push queue + storage to close cleanly, then exits. If the
daemon does not exit within that budget, the command exits 1 and
suggests ``--force``.

Forced stop:

```bash
engram daemon stop --force
```

SIGKILLs after the timeout. Use this only when a graceful stop has
already failed; the daemon's drain contract (see ``docs/adr/008-daemon-mode.md``)
handles in-flight git pushes correctly under SIGTERM.

---

## ``engram daemon logs``

Tail the last 200 lines:

```bash
engram daemon logs
```

Tail with custom count:

```bash
engram daemon logs --tail 1000
```

Follow:

```bash
engram daemon logs --follow
```

The follower reopens the file on rotation (inode-change detection)
so you don't lose lines when the log rolls.

**PII note.** By default the daemon's per-request log line redacts
thought content — it emits ``request=capture_thought fingerprint=<hex>
bytes=<int> proxy_pid=<int>`` rather than the raw text. If you flip
``daemon.log_redact_thought_content=false`` to debug a content-
related issue, ``engram daemon logs`` prints a banner warning that
the log may contain PII and should be treated accordingly.

---

## Troubleshooting

### "stale socket" warning in ``engram doctor``

The daemon left a socket file behind without a listener (most often
after SIGKILL). Run ``engram daemon start`` — the daemon's spawn
dance unlinks any stale socket before binding fresh.

### "spawn timeout" error

The daemon's startup took longer than ``daemon.spawn_timeout_seconds``
(default 30s). The most common cause is a large WAL file at startup
that needs replay; engram already pads the effective timeout by
``daemon.wal_recovery_grace_seconds`` (default 60s) when
``engram.db-wal`` exceeds 10 MiB. If your environment needs more,
bump either knob in ``engram.config.yaml``.

### "peer-cred reject" lines in the log

The daemon refused a UDS connection because the connecting process
had a different UID than the daemon. This should never happen on a
single-user machine; investigate any non-engram process touching
``<vault>/.indexes/engram.sock``.

### ``LockError`` when running ``engram serve --no-daemon``

A daemon is holding the vault lock. ``--no-daemon`` and daemon mode
are mutually exclusive. Either run ``engram daemon stop`` first, or
drop the ``--no-daemon`` flag and let the proxy attach normally.

### UDS path too long on macOS

macOS limits ``sun_path`` to 104 bytes including the trailing NUL.
If your vault directory lives under a deep path
(``/Users/.../some-very-long/path/...``), the resolver refuses with a
``DaemonError`` pointing you to the workaround: symlink the vault
into ``~/.engram-vaults/<short-name>/`` and configure engram with
that shorter path.

---

## ``DaemonConfig`` reference

Every knob lives under ``daemon:`` in the vault's
``engram.config.yaml``. Defaults are spec-mandated; the table below
documents what each knob does so operators can tune deliberately.

| Field | Default | Range | Meaning |
|---|---|---|---|
| ``auto_spawn`` | ``true`` | bool | If false, ``engram serve`` errors out when no daemon is running. Use when you run daemons via launchd / systemd-user units and want to refuse silent fallbacks. |
| ``idle_shutdown_seconds`` | ``3600`` | ``>= 0`` | Seconds after the last proxy disconnects until the daemon auto-exits. ``0`` means never. |
| ``spawn_timeout_seconds`` | ``30`` | ``>= 1`` | How long the proxy waits for ``ready\n`` from a spawning daemon. |
| ``spawn_lock_timeout_seconds`` | ``10`` | ``>= 1`` | How long the proxy waits for the per-vault spawn flock when a concurrent spawner is mid-dance. |
| ``wal_recovery_grace_seconds`` | ``60`` | ``>= 0`` | Extra grace added to ``spawn_timeout_seconds`` when the daemon detects a large WAL at startup. |
| ``shutdown_drain_seconds`` | ``5`` | ``>= 1`` | Per-connection-task force-cancel budget during shutdown drain. |
| ``coordinator_flush_seconds`` | ``30`` | ``>= 1`` | ``coordinator.force_flush`` budget. Distinct from the outer ``engram daemon stop`` 60s wait so long git pushes get their own time. |
| ``connection_idle_timeout_seconds`` | ``86400`` | ``>= 0`` | Per-connection idle timeout (24h). ``0`` means never time out an idle proxy. |
| ``max_frame_bytes`` | ``16777216`` | ``>= 65536`` | Maximum single JSON-RPC frame size (16 MiB default). The proxy mirrors this limit. |
| ``log_max_size_mb`` | ``100`` | ``>= 1`` | Rotation threshold for ``engram.log``. ``0`` not permitted; set very large to effectively disable. |
| ``log_retention_days`` | ``7`` | ``>= 1`` | Days of rotated logs to retain. |
| ``log_level`` | ``"INFO"`` | str | Daemon log level. |
| ``log_redact_thought_content`` | ``true`` | bool | When true, per-request log lines emit fingerprint + byte count only — not raw content. When false, full content is logged and ``engram daemon logs`` prints a PII warning banner. |

---

## Doctor checks

``engram doctor`` learned 6 new daemon-mode rows in v0.5.0:

- ``daemon_running`` (INFO) — is the daemon up?
- ``daemon_socket_permissions`` (WARN on non-0o600 or foreign owner)
- ``daemon_socket_stale`` (WARN on orphaned socket file)
- ``daemon_log_rotation_healthy`` (WARN when log exceeds threshold
  AND has not rotated in over 24h)
- ``daemon_uptime_excessive`` (INFO after 7 days of uptime —
  suggesting a restart to pick up engram updates)
- ``daemon_socket_path_too_long`` (WARN when the resolved UDS path
  exceeds macOS's 104-byte ``sun_path`` limit)

---

## What changes if you run ``engram serve --no-daemon``

``--no-daemon`` reverts to the pre-Phase-5 single-process behavior
bit-for-bit. Use cases:

- One-shot CLI scripts that invoke engram in-process.
- Embedded contexts where the daemon dance is unwanted overhead.
- Debugging the storage layer without proxy indirection.
- Integration tests that prefer the single-process model.

Mutual exclusion: ``--no-daemon`` acquires ``VaultLock`` directly. If
a daemon is running for that vault, ``--no-daemon`` exits with the
existing ``LockError`` message; stop the daemon first.

---

## See also

- **Design rationale**: ``docs/adr/008-daemon-mode.md`` (per-vault
  topology, auto-spawn, UDS, FastMCP dispatch trade-offs).
- **Historical plan + audits** (archived for posterity, not user-facing):
  ``docs/archive/phases/`` (the design spec, FastMCP audit, baseline
  test snapshot, and 8-layer execution plan).
