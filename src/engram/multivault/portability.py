"""Cross-vault portability defense-in-depth gate (Phase 3 Step 6).

The portability invariant pinned at the top of ``docs/PHASE_3_PLAN.md``:

1. Default cross-vault search returns ``portable`` thoughts only.
2. ``include_sensitive=True`` opts into adding ``sensitive`` thoughts.
3. ``block`` thoughts NEVER appear in cross-vault results regardless of
   any flag.
4. The LLM portability gate is a SEPARATE per-thought check (resolver in
   :mod:`engram.llm.resolver`).

The aggregator pushes (3) down at the SQL layer via a
``WHERE portability != 'block'`` predicate on every per-vault subquery.
This module is the defense-in-depth re-filter every cross-vault read
path runs after merge, so a missed push-down silently produces
nothing instead of leaking ``block`` thoughts.

Two modes are exposed:

* :func:`assert_no_block_in_results` - raises
  :class:`engram.errors.BlockThoughtLLMDisallowed` when a ``block``
  row reaches an LLM-context-assembly path. Use at LLM tool entry-points
  where the user has already opted into LLM and we should NOT silently
  drop content.
* :func:`strip_block_thoughts` - returns a filtered list with ``block``
  rows removed. Use on read-only views (cross-vault search,
  cross-vault list, ``thought_stats``) where silent dropping is the
  documented behavior - operators see by-vault counts that include
  ``block`` rows but the actual rows are never returned to the client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from engram.errors import BlockThoughtLLMDisallowed

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.models import Thought

#: Generic so callers can keep their narrower row type
#: (``Thought`` / ``ThoughtWithSimilarity`` / ``AggregatorResultRow``).
_RowT = TypeVar("_RowT")


def _is_block(row: object) -> bool:
    """Return True iff ``row.portability`` (attr or key) equals ``"block"``."""
    portability = getattr(row, "portability", None)
    if portability is None and isinstance(row, dict):
        portability = row.get("portability")
    return portability == "block"


def strip_block_thoughts(rows: Sequence[_RowT]) -> list[_RowT]:
    """Return ``rows`` with any ``portability == 'block'`` row removed.

    Pure - safe to call on the result of a search before returning to a
    client. The function never raises; the caller decides whether to log
    the strip count.
    """
    return [row for row in rows if not _is_block(row)]


def assert_no_block_in_results(rows: Sequence[object]) -> None:
    """Raise if any row has ``portability == 'block'``.

    Use at LLM-context-assembly call sites where a missed strip would
    leak ``block`` content to a remote API. The defense-in-depth gate
    composes with the SQL push-down: missing the push-down does NOT
    silently leak; this gate fires.

    Raises:
        BlockThoughtLLMDisallowed: enumerating the offending thought ids.
    """
    block_ids = []
    for row in rows:
        if _is_block(row):
            tid = getattr(row, "id", None)
            if tid is None and isinstance(row, dict):
                tid = row.get("id")
            block_ids.append(str(tid))
    if block_ids:
        msg = (
            "block-portability thought(s) reached an LLM-context-assembly "
            f"path: {block_ids}. The push-down filter at the SQL layer was "
            "bypassed; the defense-in-depth gate refuses to forward block "
            "content."
        )
        raise BlockThoughtLLMDisallowed(msg)


def split_portabilities(
    rows: Sequence[Thought],
) -> tuple[list[Thought], list[Thought], list[Thought]]:
    """Partition ``rows`` into ``(portable, sensitive, block)`` lists.

    Helper for the resolver and bundle exporter that need to act per
    portability tier without re-iterating.
    """
    portable: list[Thought] = []
    sensitive: list[Thought] = []
    block: list[Thought] = []
    for row in rows:
        if row.portability == "block":
            block.append(row)
        elif row.portability == "sensitive":
            sensitive.append(row)
        else:
            portable.append(row)
    return portable, sensitive, block
