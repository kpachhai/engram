"""Phase 2 multi-machine sync subsystem.

This package owns the sync coordinator state machine, the typed async
git wrapper (:mod:`engram.sync.gitops`), the per-vault identity check
(:mod:`engram.sync.identity`), and the startup probe surface
(:mod:`engram.sync.startup_probes`).

Phase 1 left :meth:`engram.storage.facade.VaultStorage._post_capture_sync`
as a no-op; Phase 2 wires the real coordinator in via
:func:`engram.cli.serve`. Tests instantiate :class:`VaultStorage` without
a coordinator and the no-op fallback keeps unit tests hermetic.
"""

from __future__ import annotations
