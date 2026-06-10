"""Safety gates for consolidation apply.

Apply is a daemon-stopped one-shot: it acquires the vault's ``VaultLock``
for its entire run (the same flock the daemon holds), so a mid-run MCP
connection cannot auto-spawn a daemon into a second-WAL-writer wedge - the
spawn fails cleanly against the held lock instead. There is deliberately
no ``--force`` escape.

Refusals (team-write vaults, read-only roles, cloud-synced paths) live here
so the CLI layer stays thin and each gate is unit-testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.errors import (
    ConsolidateError,
    ConsolidateVaultBusy,
    LockError,
    VaultReadOnlyError,
)
from engram.sync.startup_probes import _CLOUD_SYNC_HINTS
from engram.utils.lock import VaultLock, serve_lock_metadata

if TYPE_CHECKING:
    from pathlib import Path


def cloud_sync_hint_for(vault_path: Path) -> str | None:
    """Name of the cloud-sync provider under which the vault lives, if any.

    flock is unreliable on NFS/SMB/Dropbox/iCloud-style paths - a lock that
    silently protects nothing is worse than a refusal.
    """
    parts_lower = [part.lower() for part in vault_path.resolve().parts]
    joined = "/".join(parts_lower)
    for hint in _CLOUD_SYNC_HINTS:
        lowered = hint.lower()
        # Multi-part hints (e.g. "Library/CloudStorage") span path components.
        if ("/" in lowered and lowered in joined) or lowered in parts_lower:
            return hint
    return None


def ensure_vault_applyable(*, role: str, vault_path: Path) -> None:
    """Refuse ``--apply`` on vaults it must never mutate.

    Raises:
        VaultReadOnlyError: read-only role.
        ConsolidateError: team-write role (attribution semantics make
            curating other authors' thoughts unsafe; the server-side
            pre-receive hook would reject the push anyway), or the vault
            lives under a cloud-sync provider where flock protects nothing.
    """
    if role == "read-only":
        msg = "vault is mounted role=read-only; consolidate --apply refuses to mutate it"
        raise VaultReadOnlyError(msg)
    if role == "team-write":
        msg = (
            "consolidate --apply is not supported on team-write vaults: merging "
            "would break captured_by attribution and the team pre-receive hook "
            "would reject the push. Report mode remains available."
        )
        raise ConsolidateError(msg)
    hint = cloud_sync_hint_for(vault_path)
    if hint is not None:
        msg = (
            f"vault lives under cloud-sync provider {hint!r}, where file locking "
            "is unreliable; consolidate --apply refuses. Move the vault to a "
            "local path (git remains the supported sync mechanism)."
        )
        raise ConsolidateError(msg)


def acquire_apply_lock(vault_path: Path) -> VaultLock:
    """Acquire the vault's ``VaultLock`` for the full apply run.

    Raises:
        ConsolidateVaultBusy: the daemon (or another consolidate run) holds
            the vault. There is no ``--force``; the remediation is
            ``engram daemon stop``.
    """
    daemon_meta = serve_lock_metadata(vault_path)
    lock = VaultLock(vault_path, force=False)
    try:
        lock.acquire()
    except LockError as exc:
        if daemon_meta is not None:
            holder = daemon_meta.get("pid", "unknown")
            msg = (
                f"the vault is held by a running daemon/serve (pid {holder}); "
                "run `engram daemon stop`, re-run consolidate --apply, then "
                "restart the daemon (it auto-spawns on the next MCP connection)"
            )
        else:
            msg = (
                "another process holds the vault lock (possibly a second "
                "consolidate run); wait for it to finish and retry"
            )
        raise ConsolidateVaultBusy(msg) from exc
    return lock


__all__ = ["acquire_apply_lock", "cloud_sync_hint_for", "ensure_vault_applyable"]
