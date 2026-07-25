# Phase 5 — FastMCP Per-Connection Dispatch Audit

Captured: 2026-05-13 (Layer A task A0.5)
Spec reference: `docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md` Section 12.1; Layer C step 1 of `docs/PHASE_5_PLAN.md` selects Option A vs B based on this audit.

## Pinned version

**fastmcp 3.2.4** (Apache-2.0). Confirmed from two sources:

- `.venv/lib/python3.11/site-packages/fastmcp-3.2.4.dist-info/METADATA` → `Version: 3.2.4`
- `fastmcp/__init__.py` line 27: `__version__ = _version("fastmcp")` resolves to `3.2.4`
- `uv.lock` pins `fastmcp` at `3.2.4`
- PyPI confirms 3.2.4 is the current 3.x latest as of 2026-05-13 — **zero drift today**

## Public API surface for per-request dispatch

`FastMCP` inherits from `TransportMixin` (`fastmcp/server/mixins/transport.py`). All transport-facing methods own a loop:

| Method | File:Line | Loop ownership |
|---|---|---|
| `FastMCP.run(transport=...)` | `transport.py:77` | sync entrypoint |
| `FastMCP.run_async(transport=...)` | `transport.py:43` | async entrypoint |
| `FastMCP.run_stdio_async(show_banner, log_level, stateless)` | `transport.py:184` | owns the stdio loop |
| `FastMCP.run_http_async(...)` | `transport.py:226` | owns the HTTP server loop |

**There is no public per-request method on `FastMCP` that accepts one JSON-RPC frame and returns one response.** The closest public methods are operation-specific (`call_tool`, `list_tools`, `read_resource`, `render_prompt` on the `MCPOperationsMixin`), but they bypass the MCP session / initialize handshake / middleware routing layer, so they cannot serve a real MCP client over a socket.

## Recommended dispatch entrypoint

Use `FastMCP._mcp_server` (a `LowLevelServer` declared at `fastmcp/server/server.py:376`). Its `run` method is the transport-agnostic dispatch loop that `run_stdio_async` itself calls:

```python
# fastmcp/server/low_level.py:225
async def LowLevelServer.run(
    read_stream:  MemoryObjectReceiveStream[SessionMessage | Exception],
    write_stream: MemoryObjectSendStream[SessionMessage],
    initialization_options: InitializationOptions,
    raise_exceptions: bool = False,
    stateless: bool = False,
) -> None
```

`run_stdio_async` (`transport.py:213`) opens an anyio `stdio_server()` to get streams and then calls `self._mcp_server.run(read_stream, write_stream, ...)`. Per-connection dispatch reuses the same hook with anyio in-memory streams per accepted UDS connection — one stream pair per connection, one shared `FastMCP` / `_mcp_server` across all connections, sharing the tool registry.

`SessionMessage` lives at `mcp/shared/message.py:46` and wraps `JSONRPCMessage` plus per-message metadata.

## Confidence level

**MEDIUM** — `_mcp_server` is underscore-prefixed (a fastmcp-internal attribute), but `LowLevelServer.run` itself takes a clean, stable stream contract inherited from the upstream `mcp` SDK. `run_stdio_async` already uses exactly this contract. The pattern is upstream-MCP-canonical, not fastmcp-bespoke.

## Mitigation

Two layers absorb future fastmcp drift:

1. **Compat shim.** Layer C adds `src/engram/daemon/_fastmcp_compat.py` exposing `get_low_level_server(fastmcp) -> LowLevelServer` plus `make_session_streams()`. All daemon access to `_mcp_server` goes through this single shim — when a fastmcp bump renames or hides `_mcp_server`, only the shim changes.
2. **Pin + smoke.** `pyproject.toml` already pins `fastmcp>=2.0`; tighten the upper bound to `fastmcp>=3.2,<4` in Layer C (or in pyproject.toml as a follow-up). Add Layer G test `tests/daemon/test_dispatch_isolation.py` (Amendment 11) asserting:
   - the shim resolves a `LowLevelServer`,
   - `LowLevelServer.run` accepts the four-positional + two-kwarg signature (`read_stream`, `write_stream`, `initialization_options`, `raise_exceptions`, `stateless`),
   - two concurrent in-memory connections see only their own responses (no cross-talk on message `id`).

A fastmcp version bump that breaks any of those three assertions fails CI loudly — the daemon does not silently corrupt traffic.

## Concrete code sketch — per-connection dispatch

```python
# engram/daemon/dispatch.py (Layer C step 1, Option A)
import anyio
from mcp.server.lowlevel.server import NotificationOptions
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

async def serve_connection(server, uds_reader, uds_writer):
    # One memory stream pair PER connection. The shared FastMCP / _mcp_server
    # owns the tool registry, lifespan, middleware - all multiplexed safely.
    client_to_server_w, client_to_server_r = anyio.create_memory_object_stream(64)
    server_to_client_w, server_to_client_r = anyio.create_memory_object_stream(64)

    init_opts = server._mcp_server.create_initialization_options(
        notification_options=NotificationOptions(tools_changed=True),
    )

    async def pump_in():       # UDS bytes -> SessionMessage
        async for line in uds_reader:
            msg = JSONRPCMessage.model_validate_json(line)
            await client_to_server_w.send(SessionMessage(message=msg))

    async def pump_out():      # SessionMessage -> UDS bytes
        async for sm in server_to_client_r:
            uds_writer.write(sm.message.model_dump_json().encode() + b"\n")
            await uds_writer.drain()

    async with anyio.create_task_group() as tg:
        tg.start_soon(pump_in)
        tg.start_soon(pump_out)
        tg.start_soon(server._mcp_server.run,
                      client_to_server_r, server_to_client_w, init_opts)
```

The shared `FastMCP` is constructed once at daemon start; each accepted UDS connection calls `serve_connection(shared_server, reader, writer)` and gets its own `MiddlewareServerSession` (initialize handshake, per-session state) while reading from the same tool registry.

## Version drift risk

**Low today** (installed == latest 3.x). Forward risk: `_mcp_server` is underscore-prefixed; a fastmcp minor could rename or hide it. The `MemoryObjectStream + SessionMessage + LowLevelServer.run` contract itself is owned by the upstream `mcp` SDK, much more stable than fastmcp-side renames. The compat shim + dispatch-isolation smoke (Mitigation, above) bound the blast radius of either kind of upstream churn.

## Decision for Layer C step 1

**Choose Option A** (use FastMCP's internal `LowLevelServer.run` via the compat shim). Option B (build our own JSON-RPC parse/dispatch/serialize loop in `daemon/server.py`) is preserved as the documented fallback in Layer C step 1's module docstring; it activates only if a future fastmcp release breaks the shim AND the dispatch-isolation test cannot be repaired.

## Absolute paths referenced

- `<repo>/.venv/lib/python3.11/site-packages/fastmcp/server/server.py` (FastMCP, `_mcp_server` at line 376)
- `<repo>/.venv/lib/python3.11/site-packages/fastmcp/server/low_level.py` (`LowLevelServer.run` at line 225)
- `<repo>/.venv/lib/python3.11/site-packages/fastmcp/server/mixins/transport.py` (`run_stdio_async` at line 184 — upstream usage pattern)
- `<repo>/.venv/lib/python3.11/site-packages/mcp/shared/message.py` (`SessionMessage` at line 46)
- `<repo>/.venv/lib/python3.11/site-packages/fastmcp-3.2.4.dist-info/METADATA` (version pin)
