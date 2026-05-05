"""VaultRegistry - the resolver that binds vault names to storage.

The registry is the canonical enforcement point for two cross-vault
invariants:

1. **No path collision.** Two configured vaults whose ``path:`` differs
   textually but resolves to the same on-disk directory via
   :func:`os.path.realpath` would silently double-index the same files.
   :class:`VaultRegistry` realpath-resolves every storage's
   ``thoughts_dir`` after the mount calls and refuses to construct if any
   two distinct names map to the same realpath.
2. **Read-only-role hard refusal.** Every storage mounted under
   ``role: read-only`` has its ``read_only_role`` flag set so subsequent
   write calls (``capture``, ``update_metadata``, ``update_body``,
   ``delete``, ``repair_pending_embeddings``, ``reindex_vault``) raise
   :class:`engram.errors.VaultReadOnlyError`. Doctor surfaces the
   ``skipped`` count rather than failing the whole pass.

:class:`VaultRegistry.__init__` is the authoritative collision-detection
point; :class:`UserConfig`'s ``_check_one_primary_vault`` validator is
advisory because symlinks may change between config load and serve
startup.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Literal

from engram.errors import (
    DuplicateVaultName,
    VaultError,
    VaultPathCollision,
)

_log = logging.getLogger("engram.multivault.registry")

if TYPE_CHECKING:
    from engram.storage.facade import VaultStorage

#: Recognized role tags. The registry enforces at-most-one-primary;
#: team-write vaults may be mounted in arbitrary number alongside the
#: singleton primary; read-only mounts are also unbounded.
VaultRole = Literal["primary", "read-only", "team-write"]


class VaultRegistry:
    """In-process resolver mapping vault ``name`` -> open storage + role.

    The registry holds three parallel mappings keyed on the vault name:

    * ``_storages``: :class:`engram.storage.facade.VaultStorage` instances
      already opened by the serve startup path.
    * ``_coordinators``: optional
      :class:`engram.sync.coordinator.SyncCoordinator` per vault; the
      registry treats coordinators as opaque so this module does not
      import the sync package directly (kept loose-coupled so tests can
      construct a registry without a coordinator).
    * ``_roles``: the role each vault was mounted under
      (``"primary"`` / ``"read-only"``).

    Construction validates that:

    * At most one vault is ``role: "primary"``.
    * Every vault's ``thoughts_dir`` is unique under ``realpath``.

    Use :meth:`mount` after construction to register vaults; the
    realpath collision check runs at every mount call so the property
    holds after every state change.
    """

    def __init__(self) -> None:
        """Construct an empty registry. Mount vaults via :meth:`mount`."""
        self._storages: dict[str, VaultStorage] = {}
        self._coordinators: dict[str, object | None] = {}
        self._roles: dict[str, VaultRole] = {}

    def mount(
        self,
        *,
        name: str,
        storage: VaultStorage,
        role: VaultRole,
        coordinator: object | None = None,
    ) -> None:
        """Register a vault under ``name``.

        Args:
            name: Logical vault name (used in
                :class:`engram.models.mcp.Filter.vault` and search results).
            storage: An open :class:`engram.storage.facade.VaultStorage`
                whose ``thoughts_dir`` is unique among all currently
                mounted vaults under :func:`os.path.realpath`.
            role: ``"primary"`` for the local vault that accepts captures;
                ``"read-only"`` for friend-share or work-machine pull-only
                mounts.
            coordinator: Optional sync coordinator for this vault. Passed
                through ``set_sync_coordinator`` on the storage so capture
                still drives the per-vault commit pipeline.

        Raises:
            DuplicateVaultName: if ``name`` is already mounted.
            VaultPathCollision: if ``storage.thoughts_dir`` resolves to the
                same realpath as an already-mounted vault.
            VaultError: if mounting a second ``primary`` vault.
        """
        if name in self._storages:
            msg = f"vault name {name!r} already mounted"
            raise DuplicateVaultName(msg)

        if role == "primary":
            existing_primary = [n for n, r in self._roles.items() if r == "primary"]
            if existing_primary:
                msg = (
                    f"a primary vault is already mounted: {existing_primary[0]!r}; "
                    f"refusing to mount {name!r} also as primary"
                )
                raise VaultError(msg)

        self._assert_no_realpath_collision(name=name, storage=storage)

        self._storages[name] = storage
        self._coordinators[name] = coordinator
        self._roles[name] = role

        # read-only is the only role that hard-refuses writes at the
        # storage layer. team-write keeps writes permitted; the team
        # policy gate runs at capture-time.
        storage.set_read_only_role(read_only=(role == "read-only"))
        if coordinator is not None:
            # Defensive: set_sync_coordinator stores the reference and
            # never raises in the current implementation; tolerate any
            # subclass override that does.
            with contextlib.suppress(Exception):  # pragma: no cover
                storage.set_sync_coordinator(coordinator)

    def unmount(self, name: str) -> None:
        """Detach a vault from the registry.

        The registry closes the storage's connection on unmount; callers
        that need the storage open after detaching must re-mount or open a
        new instance.
        """
        storage = self._storages.pop(name, None)
        coordinator = self._coordinators.pop(name, None)
        self._roles.pop(name, None)
        if storage is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                storage.close()
        if coordinator is not None and hasattr(coordinator, "stop"):
            with contextlib.suppress(Exception):  # pragma: no cover
                coordinator.stop()

    def get(self, name: str) -> VaultStorage | None:
        """Return the storage for ``name`` or ``None`` if not mounted."""
        return self._storages.get(name)

    def role_of(self, name: str) -> VaultRole | None:
        """Return the role for ``name`` or ``None`` if not mounted."""
        return self._roles.get(name)

    def coordinator_of(self, name: str) -> object | None:
        """Return the coordinator object passed at mount time (or None)."""
        return self._coordinators.get(name)

    def primary(self) -> VaultStorage:
        """Return the singleton primary vault's storage.

        Raises:
            VaultError: if zero primary vaults are mounted, or more than
                one (defense-in-depth: :meth:`mount` already refuses a
                second primary, but the no-zero case is also load-bearing).
        """
        primaries = [n for n, role in self._roles.items() if role == "primary"]
        if not primaries:
            msg = "no primary vault mounted; cannot resolve primary()"
            raise VaultError(msg)
        if len(primaries) > 1:  # pragma: no cover - mount() refuses this
            msg = f"more than one primary vault mounted: {sorted(primaries)}"
            raise VaultError(msg)
        return self._storages[primaries[0]]

    def primary_name(self) -> str:
        """Return the primary vault's name (or raise if none mounted)."""
        primaries = [n for n, role in self._roles.items() if role == "primary"]
        if not primaries:
            msg = "no primary vault mounted"
            raise VaultError(msg)
        return primaries[0]

    def read_only_vaults(self) -> set[str]:
        """Return the set of vault names mounted as ``role: read-only``."""
        return {n for n, role in self._roles.items() if role == "read-only"}

    def names(self) -> list[str]:
        """Return mounted vault names in stable insertion order."""
        return list(self._storages.keys())

    def __len__(self) -> int:
        """Return the count of mounted vaults."""
        return len(self._storages)

    def __contains__(self, name: object) -> bool:
        """``name in registry`` membership check."""
        return isinstance(name, str) and name in self._storages

    def iter_storages(self) -> Iterator[tuple[str, VaultStorage, VaultRole]]:
        """Iterate ``(name, storage, role)`` tuples in mount order."""
        for name, storage in self._storages.items():
            yield name, storage, self._roles[name]

    def storages_for_filter(self, filter_vault: object) -> list[tuple[str, VaultStorage]]:
        """Resolve a Filter.vault value (str / list / "*") to mounted vaults.

        Helper for :func:`engram.multivault.aggregator.aggregate_search`
        and the multi-vault tool handlers in
        :mod:`engram.mcp.server`. Substring/prefix matching is *not*
        applied (vault filters are exact-match-only).

        Args:
            filter_vault: One of ``None`` (route to primary only),
                ``"*"`` (all mounted vaults), a single name (just that
                vault), or an iterable of names.

        Returns:
            A list of ``(name, storage)`` tuples in registry insertion
            order. Names that are not mounted are silently dropped (the
            caller may compare ``len()`` against the input list to detect
            a missing vault).
        """
        if filter_vault is None:
            primary_name = self.primary_name() if self._roles else None
            if primary_name is None:
                return []
            storage = self._storages[primary_name]
            return [(primary_name, storage)]

        if isinstance(filter_vault, str):
            if filter_vault == "*":
                return [(n, s) for n, s, _ in self.iter_storages()]
            single = self._storages.get(filter_vault)
            return [] if single is None else [(filter_vault, single)]

        if isinstance(filter_vault, Iterable):
            requested = list(filter_vault)
            return [
                (name, self._storages[name])
                for name in requested
                if isinstance(name, str) and name in self._storages
            ]

        return []

    def close_all(self) -> None:
        """Close every mounted storage and drop all entries.

        Used by ``engram serve`` shutdown. Idempotent.
        """
        for name in list(self._storages.keys()):
            self.unmount(name)

    # === internals ===

    def _assert_no_realpath_collision(
        self,
        *,
        name: str,
        storage: VaultStorage,
    ) -> None:
        """Refuse if ``storage.thoughts_dir`` realpaths to an existing vault."""
        try:
            candidate = os.path.realpath(storage.thoughts_dir)
        except OSError as exc:  # pragma: no cover - filesystem availability
            msg = f"could not resolve {storage.thoughts_dir} for vault {name!r}: {exc}"
            raise VaultError(msg) from exc

        for other_name, other_storage in self._storages.items():
            try:
                other = os.path.realpath(other_storage.thoughts_dir)
            except OSError:  # pragma: no cover
                continue
            if other == candidate:
                msg = (
                    f"vault path collision (after realpath): {name!r} and "
                    f"{other_name!r} both resolve to {candidate}"
                )
                raise VaultPathCollision(msg)
