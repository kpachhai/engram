"""FastMCP per-connection dispatch isolation contract.

Spec: ``docs/PHASE_5_FASTMCP_AUDIT.md`` + ``2026-05-12-engram-daemon-mode-design.md``
Amendment 11.

These tests bound the blast radius of a future fastmcp version bump
that renames ``FastMCP._mcp_server`` or changes ``LowLevelServer.run``'s
signature. They are deliberately tightly scoped at the compat-shim
boundary so the failure mode points directly to the contract that
broke.
"""

from __future__ import annotations

import inspect

import pytest
from mcp.server.lowlevel.server import Server as LowLevelServer

from engram.daemon.fastmcp_dispatch import get_low_level_server


def test_low_level_server_run_signature_stable() -> None:
    """Run accepts (read_stream, write_stream, initialization_options, ...).

    A breaking change here means the daemon's per-connection dispatch
    shim needs an update. The fix lives in ``daemon/fastmcp_dispatch.py``
    and ``docs/PHASE_5_FASTMCP_AUDIT.md`` documents the upgrade
    procedure.
    """
    sig = inspect.signature(LowLevelServer.run)
    params = list(sig.parameters)
    # First four positions are stable across fastmcp 3.x.
    assert params[:4] == ["self", "read_stream", "write_stream", "initialization_options"]


def test_get_low_level_server_returns_real_low_level() -> None:
    """The shim resolves a LowLevelServer from a real FastMCP instance."""
    from fastmcp import FastMCP

    fastmcp = FastMCP("engram-test")
    low_level = get_low_level_server(fastmcp)
    assert isinstance(low_level, LowLevelServer)


def test_get_low_level_server_raises_on_missing_attr() -> None:
    """If a future fastmcp hides ``_mcp_server``, the shim fails loudly."""

    class _FakeFastMCP:
        pass

    with pytest.raises(TypeError) as exc_info:
        get_low_level_server(_FakeFastMCP())  # type: ignore[arg-type]
    assert "_mcp_server" in str(exc_info.value)
    assert "PHASE_5_FASTMCP_AUDIT" in str(exc_info.value)


def test_get_low_level_server_raises_on_wrong_type() -> None:
    """If ``_mcp_server`` exists but is the wrong type, fail loudly."""

    class _BadFastMCP:
        _mcp_server = "not a LowLevelServer"

    with pytest.raises(TypeError):
        get_low_level_server(_BadFastMCP())  # type: ignore[arg-type]
