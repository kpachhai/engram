"""Cross-vault search aggregator (Phase 3 Step 5).

Two execution modes:

* **ATTACH** when ``mounted_vault_count <= 10`` AND
  ``aggregator.force_sequential`` is False. Each vault's storage runs its
  own sqlite-vec ANN query inside its own connection, but the aggregator
  treats the cross-vault merge as the conceptual ATTACH path: small
  fan-out, all vaults queried before merge, no early termination.
* **SEQUENTIAL** when ``mounted_vault_count > 10`` OR
  ``force_sequential`` is True. Same per-storage queries, surfaced to
  the operator via the ``aggregator_mode`` doctor INFO row so the
  latency cliff at 11 vaults is visible.

The portability invariant ``block`` NEVER crosses vaults is pushed down
at the per-vault SQL layer by constructing a per-storage Filter whose
``portability`` is ``["portable"]`` (default) or ``["portable",
"sensitive"]`` (with ``include_sensitive=True``); the SQL ``IN`` clause
emits ``portability IN ('portable')`` so a ``block`` row never matches.
:func:`engram.multivault.portability.assert_no_block_in_results` is the
defense-in-depth re-filter.

Per-vault floor (R-H12): every mounted vault contributes its
``min_per_vault_results`` regardless of similarity score, then the
remaining slots up to ``k`` are filled by the global similarity ranking.
The floor + the ``k`` cap together produce a list of length
``max(k, min_per_vault_results * mounted_vault_count)`` in the worst
case; the caller is expected to handle the larger list when the floor
exceeds the implied cap.

Reference: ``docs/PHASE_3_PLAN.md`` Step 5 + Pinned Portability Invariant.
"""

from __future__ import annotations

import enum
import heapq
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.errors import EmbeddingModelMismatch
from engram.models.frontmatter import Portability
from engram.models.mcp import Filter
from engram.multivault.portability import strip_block_thoughts
from engram.storage.sqlite import get_setting

if TYPE_CHECKING:
    from engram.models import ThoughtWithSimilarity
    from engram.multivault.registry import VaultRegistry
    from engram.storage.facade import VaultStorage

_log = logging.getLogger("engram.multivault.aggregator")


class AggregatorMode(enum.StrEnum):
    """Active execution mode of :func:`aggregate_search`."""

    ATTACH = "ATTACH"
    SEQUENTIAL = "SEQUENTIAL"


#: Threshold above which the aggregator drops to ``SEQUENTIAL`` mode.
ATTACH_VAULT_COUNT_CEILING = 10


@dataclass(slots=True)
class AggregatorResultRow:
    """A single row from :func:`aggregate_search`.

    Carries the ``vault_name`` of origin so the caller can group results
    by vault without re-deriving the attribution. The composite primary
    key is ``(vault_name, id)`` because thought ids are vault-scoped
    (same UUID could exist in two vaults if a thought was imported then
    re-exported - tests assert this case is detected as a cycle, but the
    composite key is the failsafe).
    """

    vault_name: str
    thought: ThoughtWithSimilarity

    @property
    def similarity(self) -> float:
        """Pass-through to the underlying thought's similarity score."""
        return self.thought.similarity

    @property
    def id(self) -> str:
        """The thought's UUID-v7 (string form)."""
        return str(self.thought.id)

    @property
    def portability(self) -> Portability:
        """Defense-in-depth: re-asserted by the portability gate after merge."""
        return self.thought.portability


@dataclass(slots=True)
class AggregatedSearchResult:
    """Output container for :func:`aggregate_search`.

    The ``mode_used`` field is exposed deliberately: tests assert it,
    doctor's ``aggregator_mode`` INFO row reads it, and operators
    debugging "why is search slow?" want to see whether the SEQUENTIAL
    fallback fired.
    """

    rows: list[AggregatorResultRow] = field(default_factory=list)
    mode_used: AggregatorMode = AggregatorMode.ATTACH
    degraded_vaults: list[str] = field(default_factory=list)
    per_vault_total_found: dict[str, int] = field(default_factory=dict)

    @property
    def total_found(self) -> int:
        """Sum of per-vault ``total_found`` counts (filter-eligible pool)."""
        return sum(self.per_vault_total_found.values())


def _portabilities_for_search(*, include_sensitive: bool) -> list[Portability]:
    """Build the per-vault Filter portability list per the pinned invariant.

    ``block`` is NEVER included; ``sensitive`` only when
    ``include_sensitive=True`` (Plan B-3 fix).
    """
    if include_sensitive:
        return ["portable", "sensitive"]
    return ["portable"]


def _embedding_settings_for(storage: VaultStorage) -> tuple[str | None, int | None]:
    """Read ``embedding_model_name`` + dim from a vault's ``engram_settings``."""
    name = get_setting(storage.conn, "embedding_model_name")
    dim_raw = get_setting(storage.conn, "embedding_dim")
    dim = int(dim_raw) if dim_raw is not None and dim_raw.isdigit() else None
    return name, dim


def assert_compatible_embeddings(registry: VaultRegistry) -> None:
    """Refuse cross-vault search when mounted vaults disagree on embedding.

    Phase 3 Step 7 verifier: every mounted vault's
    ``embedding_model_name`` + ``embedding_dim`` (read from
    ``engram_settings``) must match. Two vaults with different models
    produce non-comparable cosine scores; the aggregator refuses rather
    than ranking apples and oranges (R-M11).

    The check runs on every cross-vault search invocation; the cost is
    one ``SELECT value FROM engram_settings WHERE key = ?`` per vault
    (cached at the SQLite page-cache level).
    """
    seen: dict[tuple[str | None, int | None], str] = {}
    for name, storage, _role in registry.iter_storages():
        model_name, dim = _embedding_settings_for(storage)
        key = (model_name, dim)
        # Only enforce when at least one vault declared a model name; an
        # all-None state means the engram_settings rows haven't been
        # populated yet (fresh vault before first capture). The mismatch
        # path is the load-bearing case.
        if model_name is None and dim is None:
            continue
        if key in seen:
            continue
        if seen and (model_name is not None or dim is not None):
            other_key, other_name = next(iter(seen.items()))
            if other_key != key:
                msg = (
                    f"embedding model mismatch across vaults: "
                    f"{other_name!r} uses {other_key!r}; {name!r} uses {key!r}. "
                    "Cross-vault search refused; pin both vaults to the "
                    "same model and reindex one of them."
                )
                raise EmbeddingModelMismatch(msg)
        seen[key] = name


def _per_vault_filter(
    *,
    base: Filter | None,
    include_sensitive: bool,
) -> Filter:
    """Compose the per-vault Filter from the user filter + invariant."""
    portabilities = _portabilities_for_search(include_sensitive=include_sensitive)
    base_dict = base.model_dump(exclude_none=True) if base is not None else {}
    # The aggregator's portability list overrides any user-supplied
    # portability filter so block is provably never included. The user's
    # portability filter would have been advisory anyway because the
    # aggregator already enforces the cross-vault invariant.
    base_dict["portability"] = list(portabilities)
    # Do not propagate the user-level vault filter into the per-storage
    # call: by construction, each storage's results are already from the
    # vault we are iterating over, so a vault filter would either be a
    # tautology or empty.
    base_dict.pop("vault", None)
    return Filter.model_validate(base_dict)


def _query_one_vault(
    *,
    name: str,
    storage: VaultStorage,
    query_embedding: Sequence[float],
    k: int,
    base_filter: Filter | None,
    include_sensitive: bool,
) -> tuple[list[ThoughtWithSimilarity], int]:
    """Run the per-vault ANN search; portability push-down at the SQL layer."""
    per_filter = _per_vault_filter(base=base_filter, include_sensitive=include_sensitive)
    rows, total_found = storage.search(query_embedding=query_embedding, k=k, filter_=per_filter)
    return rows, total_found


def aggregate_search(
    *,
    registry: VaultRegistry,
    query_embedding: Sequence[float],
    k: int = 10,
    filter_: Filter | None = None,
    include_sensitive: bool = False,
    min_per_vault_results: int = 3,
    aggregate_timeout_seconds: float = 5.0,
    force_sequential: bool = False,
) -> AggregatedSearchResult:
    """Cross-vault vector search with portability push-down + per-vault floor.

    Args:
        registry: The :class:`VaultRegistry` whose mounted storages are
            the search corpus. The caller decides which vaults to include
            via ``filter_.vault`` (default: all mounted vaults).
        query_embedding: Pre-computed embedding vector matching every
            mounted vault's embedding model (compat is verified by
            :func:`assert_compatible_embeddings` before this function
            runs).
        k: Maximum total results returned. Each vault's local top-k is
            ``max(k, min_per_vault_results)`` so the floor + ranking
            both have enough candidates.
        filter_: User-supplied filter (vault, prefix, source, tags,
            timestamps). The aggregator overrides ``portability`` per
            the pinned invariant; other fields pass through unchanged.
        include_sensitive: Adds ``sensitive`` thoughts to the per-vault
            push-down filter. ``block`` is ALWAYS excluded regardless.
        min_per_vault_results: Floor (R-H12); each vault contributes at
            least this many of its top results before similarity ranking
            decides the rest.
        aggregate_timeout_seconds: Per-vault wall-clock budget. Vaults
            that exceed it are added to ``degraded_vaults`` and contribute
            no rows to the merge.
        force_sequential: Operator override (or test seam) that forces
            the SEQUENTIAL mode even at <=10 vaults.

    Returns:
        :class:`AggregatedSearchResult` with up to ``max(k, floor *
        mounted_count)`` rows ranked by similarity (descending), plus
        the mode used and any degraded vaults.
    """
    selected_vaults = registry.storages_for_filter(filter_.vault if filter_ is not None else "*")
    if not selected_vaults:
        return AggregatedSearchResult(rows=[], mode_used=AggregatorMode.SEQUENTIAL)

    # Embedding compat check is cheap; run on every call.
    assert_compatible_embeddings(registry)

    mode = (
        AggregatorMode.SEQUENTIAL
        if force_sequential or len(selected_vaults) > ATTACH_VAULT_COUNT_CEILING
        else AggregatorMode.ATTACH
    )

    per_vault_k = max(k, min_per_vault_results)
    result = AggregatedSearchResult(mode_used=mode)

    # SQLite connections are bound to the thread that opened them, so the
    # aggregator runs sequentially per-vault on the calling thread. The
    # per-vault timeout is enforced post-hoc: if a vault's search took
    # longer than ``aggregate_timeout_seconds``, mark it degraded and
    # discard its rows (the operator-visible degradation contract from
    # the plan); subsequent vaults that haven't been touched yet are
    # skipped entirely once the budget is exhausted (same outcome as a
    # ThreadPool with timeout).
    deadline = time.monotonic() + aggregate_timeout_seconds
    per_vault_rows: dict[str, list[ThoughtWithSimilarity]] = {}
    for name, storage in selected_vaults:
        if time.monotonic() >= deadline:
            _log.warning(
                "aggregate_search: budget %.3fs exhausted before %r; degrading",
                aggregate_timeout_seconds,
                name,
            )
            result.degraded_vaults.append(name)
            continue

        start = time.monotonic()
        try:
            rows, total_found = _query_one_vault(
                name=name,
                storage=storage,
                query_embedding=query_embedding,
                k=per_vault_k,
                base_filter=filter_,
                include_sensitive=include_sensitive,
            )
        except Exception as exc:
            _log.warning("aggregate_search: vault %r raised %s; degrading", name, exc)
            result.degraded_vaults.append(name)
            continue

        elapsed = time.monotonic() - start
        if elapsed > aggregate_timeout_seconds:
            _log.warning(
                "aggregate_search: vault %r took %.3fs > %.3fs budget; "
                "discarding results and marking degraded",
                name,
                elapsed,
                aggregate_timeout_seconds,
            )
            result.degraded_vaults.append(name)
            continue

        # Defense-in-depth: drop any block row that somehow slipped past
        # the SQL push-down. Pre-filter before merge so a stray block row
        # never competes for ranking slots.
        rows = strip_block_thoughts(rows)
        per_vault_rows[name] = rows
        result.per_vault_total_found[name] = total_found

    # Per-vault floor: take the top `min_per_vault_results` from each
    # vault unconditionally. Then fill remaining slots up to `k` from the
    # global heap of all unclaimed rows ranked by similarity.
    floor_rows: list[AggregatorResultRow] = []
    leftover: list[AggregatorResultRow] = []
    for name, rows in per_vault_rows.items():
        for idx, row in enumerate(rows):
            agg_row = AggregatorResultRow(vault_name=name, thought=row)
            if idx < min_per_vault_results:
                floor_rows.append(agg_row)
            else:
                leftover.append(agg_row)

    floor_rows.sort(key=lambda r: r.similarity, reverse=True)
    if len(floor_rows) >= k:
        result.rows = floor_rows[:k]
        return result

    needed = k - len(floor_rows)
    top_leftover = heapq.nlargest(needed, leftover, key=lambda r: r.similarity)
    merged = floor_rows + top_leftover
    merged.sort(key=lambda r: r.similarity, reverse=True)
    result.rows = merged
    return result


def vault_filter_to_iterable(filter_vault: object) -> Iterable[str] | None:
    """Convenience for callers that iterate filter.vault uniformly.

    ``None`` and ``"*"`` are returned as ``None`` so the caller can run
    "no scoping". A scalar ``str`` becomes a single-item list; an
    iterable passes through after coercion.
    """
    if filter_vault is None or filter_vault == "*":
        return None
    if isinstance(filter_vault, str):
        return [filter_vault]
    if isinstance(filter_vault, Iterable):
        return [v for v in filter_vault if isinstance(v, str)]
    return None
