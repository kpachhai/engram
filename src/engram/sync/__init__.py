"""Multi-machine sync subsystem.

This package owns the sync coordinator state machine, the typed async
git wrapper (:mod:`engram.sync.gitops`), the per-vault identity check
(:mod:`engram.sync.identity`), and the startup probe surface
(:mod:`engram.sync.startup_probes`).

:meth:`engram.storage.facade.VaultStorage._post_capture_sync` is a no-op
when no coordinator is wired in; :func:`engram.cli.serve` wires in the
real coordinator. Tests instantiate :class:`VaultStorage` without a
coordinator and the no-op fallback keeps unit tests hermetic.
"""

from __future__ import annotations
