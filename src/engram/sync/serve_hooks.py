"""Helpers wired into ``engram serve`` lifecycle.

Step 10 deliverable. Centralizes the startup-pull logic so both
:func:`engram.cli.serve` AND test fixtures can call the same code without
having to invoke the full CLI Typer entrypoint.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from engram.config.models import SyncConfig
from engram.sync import gitops
from engram.sync.gitops import GitErrorClass, PullResult

_log = logging.getLogger("engram.sync.serve_hooks")


async def maybe_startup_pull(
    repo_dir: Path,
    sync_config: SyncConfig,
) -> PullResult | None:
    """Run ``git pull --rebase`` on startup when ``auto_pull_on_startup`` is set.

    Returns ``None`` if the pull was skipped (disabled, no remote configured,
    or sync.disabled=True). Returns the :class:`PullResult` otherwise so the
    caller can log details.
    """
    if sync_config.disabled:
        _log.info("sync disabled by config; skipping startup pull")
        return None
    if not sync_config.auto_pull_on_startup:
        return None
    url = await gitops.remote_url(repo_dir, sync_config.git_remote)
    if url is None:
        _log.info("no remote %s configured; skipping startup pull", sync_config.git_remote)
        return None
    refusal = await gitops.signed_pull_gate(
        repo_dir,
        remote=sync_config.git_remote,
        branch=sync_config.git_branch,
        signed_pull_required=sync_config.signed_pull_required,
        timeout=sync_config.startup_pull_timeout_seconds,
    )
    if refusal is not None:
        _log.warning("startup pull refused by signed-pull gate: %s", refusal)
        return PullResult(
            error_class=GitErrorClass.SIGNATURE_UNVERIFIED,
            stderr=refusal,
        )
    _log.info(
        "running startup pull --rebase against %s/%s (timeout=%.1fs)",
        sync_config.git_remote,
        sync_config.git_branch,
        sync_config.startup_pull_timeout_seconds,
    )
    try:
        result = await asyncio.wait_for(
            gitops.pull_rebase(
                repo_dir,
                sync_config.git_remote,
                sync_config.git_branch,
                timeout=sync_config.startup_pull_timeout_seconds,
            ),
            timeout=sync_config.startup_pull_timeout_seconds + 1.0,
        )
    except TimeoutError:
        _log.warning(
            "startup pull exceeded %.1fs; continuing without pull",
            sync_config.startup_pull_timeout_seconds,
        )
        return PullResult(error_class=GitErrorClass.NETWORK_TRANSIENT, stderr="local timeout")

    if result.error_class is not GitErrorClass.OK:
        _log.warning(
            "startup pull failed (%s); continuing with local state: %s",
            result.error_class.value,
            result.stderr.strip()[:200],
        )
    return result


__all__ = ["maybe_startup_pull"]
