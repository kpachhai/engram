"""VaultLock with ``install_signal_handlers=False``.

The daemon (Phase 5 Layer C) owns its own SIGTERM/SIGINT handler so the
holder must NOT install its own. Test:

1. Default behavior unchanged: VaultLock installs SIGTERM/SIGINT handlers
   when used directly (`engram serve --no-daemon` continues to work).
2. ``install_signal_handlers=False`` leaves the existing handlers alone so
   the daemon can wire its own.
"""

from __future__ import annotations

import signal
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.utils.lock import VaultLock


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """A short-path vault directory so signal-handler tests stay isolated from each other."""
    with tempfile.TemporaryDirectory(prefix="eng-vault-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        vault.mkdir()
        yield vault


def test_install_signal_handlers_default_true(short_vault: Path) -> None:
    """Default behavior: VaultLock installs its own SIGTERM/SIGINT handlers."""
    original_sigterm = signal.getsignal(signal.SIGTERM)
    with VaultLock(short_vault):
        # Handler should be overridden while the lock is held.
        assert signal.getsignal(signal.SIGTERM) is not original_sigterm
    # On release, original handler is restored.
    assert signal.getsignal(signal.SIGTERM) is original_sigterm


def test_install_signal_handlers_false_leaves_handlers_alone(short_vault: Path) -> None:
    """Daemon use case: VaultLock does NOT touch signal handlers."""
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)
    with VaultLock(short_vault, install_signal_handlers=False):
        # Handlers unchanged for both SIGTERM and SIGINT.
        assert signal.getsignal(signal.SIGTERM) is original_sigterm
        assert signal.getsignal(signal.SIGINT) is original_sigint
    # Still unchanged after release (no-op restore is fine).
    assert signal.getsignal(signal.SIGTERM) is original_sigterm
    assert signal.getsignal(signal.SIGINT) is original_sigint
