"""Multi-vault primitives.

This package provides the abstractions that let one ``engram serve``
process surface N vaults (one ``primary`` + many ``read-only``) under
different roles, perform cross-vault search with the portability invariant
``block`` NEVER leaks across vaults, and refuse writes against read-only
vaults at the storage boundary.

The major surfaces are:

* :class:`engram.multivault.registry.VaultRegistry` - the resolver that
  binds a vault ``name`` to a :class:`engram.storage.facade.VaultStorage`
  + optional :class:`engram.sync.coordinator.SyncCoordinator`. Mounts are
  realpath-checked at construction so two configured vaults that resolve
  to the same on-disk directory are refused.
* :class:`engram.multivault.aggregator.AggregatedSearchResult` and
  :func:`engram.multivault.aggregator.aggregate_search` - cross-vault
  search with portability push-down at the SQL layer + a per-vault floor
  so small vaults always contribute.
* :func:`engram.multivault.portability.assert_no_block_in_results` - the
  defense-in-depth re-filter every cross-vault read path runs before
  returning rows.
"""

from __future__ import annotations

from engram.multivault.aggregator import (
    AggregatedSearchResult,
    AggregatorMode,
    AggregatorResultRow,
    aggregate_search,
)
from engram.multivault.portability import (
    assert_no_block_in_results,
    strip_block_thoughts,
)
from engram.multivault.registry import VaultRegistry

__all__ = [
    "AggregatedSearchResult",
    "AggregatorMode",
    "AggregatorResultRow",
    "VaultRegistry",
    "aggregate_search",
    "assert_no_block_in_results",
    "strip_block_thoughts",
]
