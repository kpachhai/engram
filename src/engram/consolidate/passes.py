"""Detection passes: orchestrate clustering, staleness, and contradiction scans.

``generate_report`` is dependency-injected: content loading and LLM judgment
arrive as callables, so the pass logic stays hermetic under test and the CLI
wires real implementations (markdown reads, ``resolve_provider``-backed
judge/distiller) at the edge.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from engram.consolidate.clustering import (
    Cluster,
    cosine_matrix,
    degenerate_cluster_indexes,
    ensure_clusterable_size,
    greedy_partition,
)
from engram.consolidate.models import (
    ClusterAction,
    ClusterProposal,
    ConsolidationReport,
    ContradictionCandidate,
    ContradictionVerdict,
    ExclusionCounts,
    PassState,
    PassStatus,
    PinnedThought,
    StaleCandidate,
)
from engram.consolidate.pairs import contradiction_band_pairs
from engram.consolidate.staleness import effective_age, is_future_dated
from engram.errors import BlockThoughtLLMDisallowed, LLMProviderError
from engram.llm.budget import estimate_tokens
from engram.storage.sqlite import SETTING_EMBEDDING_MODEL_NAME, get_setting
from engram.storage.sqlite_queries import fetch_all_embeddings, list_all_thought_rows

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

#: Loads a thought's body text by id (CLI wires markdown reads; tests a dict).
ContentLoader = Callable[[str], str]
#: Judges one pair: ``(first_content, second_content) -> (verdict, rationale)``
#: where verdict is "contradiction" | "unclear" | "consistent" | "oversized"
#: ("oversized" = the pair exceeds the provider context; skipped, never
#: truncated into a verdict).
JudgeFn = Callable[[str, str], tuple[str, str]]
#: Distills a cluster: ``(members as (id, content) pairs, prefix) -> draft``.
DistillFn = Callable[[list[tuple[str, str]], str], str]

_PORTABILITY_RANK = {"portable": 0, "sensitive": 1, "block": 2}


@dataclass(frozen=True)
class ReportSettings:
    """Tunable thresholds for one consolidation run."""

    near_dup_threshold: float = 0.93
    contradiction_threshold: float = 0.75
    stale_days: int = 180
    max_cluster_size: int = 12
    contradiction_pair_cap: int = 64
    #: Conservative single-prompt token budget for cluster distillation.
    max_distill_tokens: int = 6000
    prefix: str | None = None
    #: Prefixes the similarity passes skip (near-dup clustering and
    #: contradiction judging); exact-duplicate and staleness passes still
    #: cover them. Log-like prefixes cluster on shared structure, not shared
    #: meaning, so merging them destroys history. Ignored when ``prefix``
    #: scopes the run explicitly.
    exclude_prefixes: tuple[str, ...] = ("Session Summary",)


def most_restrictive_portability(values: list[str]) -> str:
    """Block > sensitive > portable; a merged thought inherits the strictest."""
    return max(values, key=lambda v: _PORTABILITY_RANK.get(v, 0))


def cluster_id_for(fingerprints: list[str]) -> str:
    """Deterministic cluster id from member fingerprints (stable across runs)."""
    digest = hashlib.sha256("|".join(sorted(fingerprints)).encode("utf-8")).hexdigest()
    return f"c-{digest[:12]}"


def _row_dt(row: dict[str, Any], key: str) -> datetime:
    from datetime import datetime as _dt

    value = row[key]
    if isinstance(value, _dt):
        return value
    return _dt.fromisoformat(str(value))


def _row_dt_optional(row: dict[str, Any], key: str) -> datetime | None:
    if row.get(key) is None:
        return None
    return _row_dt(row, key)


def _pin(row: dict[str, Any]) -> PinnedThought:
    return PinnedThought(thought_id=UUID(str(row["id"])), fingerprint=str(row["fingerprint"]))


def generate_report(
    *,
    conn: sqlite3.Connection,
    vault_name: str,
    configured_model: str | None,
    now: datetime,
    settings: ReportSettings,
    content_loader: ContentLoader,
    judge: JudgeFn | None,
    distiller: DistillFn | None,
) -> ConsolidationReport:
    """Run all four detection passes and assemble the report.

    ``judge`` / ``distiller`` are ``None`` when LLM use is disabled
    (``--no-llm`` or no provider configured): the contradiction pass is
    skipped and every non-exact cluster degrades to manual-review.

    Raises:
        ConsolidateModelMismatch: index was embedded under a different model.
        ConsolidateVaultTooLarge: beyond the supported clustering size.
    """
    recorded_model = get_setting(conn, SETTING_EMBEDDING_MODEL_NAME)
    if (
        recorded_model is not None
        and configured_model is not None
        and (recorded_model != configured_model)
    ):
        from engram.errors import ConsolidateModelMismatch

        msg = (
            f"index embedded under {recorded_model!r} but configured model is "
            f"{configured_model!r}; run `engram reindex --full` first"
        )
        raise ConsolidateModelMismatch(msg)

    rows = list_all_thought_rows(conn, prefix=settings.prefix)
    by_id: dict[str, dict[str, Any]] = {str(row["id"]): row for row in rows}

    exclusions = ExclusionCounts(
        pending_embeddings=sum(1 for r in rows if r["embedding_status"] == "pending"),
        failed_embeddings=sum(1 for r in rows if r["embedding_status"] == "failed"),
    )

    # Pass 1a: exact duplicates by fingerprint (no embeddings needed).
    clusters: list[ClusterProposal] = []
    consumed: set[str] = set()
    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_fingerprint.setdefault(str(row["fingerprint"]), []).append(row)
    for fingerprint, group in sorted(by_fingerprint.items()):
        if len(group) < 2:
            continue
        newest = max(group, key=lambda r: (_row_dt(r, "created_at"), str(r["id"])))
        clusters.append(
            ClusterProposal(
                cluster_id=cluster_id_for([fingerprint] * len(group)),
                action=ClusterAction.KEEP_NEWEST,
                prefix=str(group[0]["prefix"]),
                members=[_pin(r) for r in group],
                similarity_floor=1.0,
                portability=most_restrictive_portability(  # type: ignore[arg-type]
                    [str(r["portability"]) for r in group]
                ),
                keep_thought_id=UUID(str(newest["id"])),
            )
        )
        consumed.update(str(r["id"]) for r in group)

    # Pass 1b: near-duplicate clustering over ok-embedding, unconsumed rows.
    embeddings = fetch_all_embeddings(conn)
    eligible = [
        row for row in rows if str(row["id"]) in embeddings and str(row["id"]) not in consumed
    ]
    ensure_clusterable_size(len(eligible))
    excluded_prefixes = set(settings.exclude_prefixes) if settings.prefix is None else set()
    prefix_excluded = 0
    raw_clusters: list[tuple[Cluster, bool]] = []  # (cluster, degenerate)
    by_prefix: dict[str, list[dict[str, Any]]] = {}
    for row in eligible:
        row_prefix = str(row["prefix"])
        if row_prefix in excluded_prefixes:
            prefix_excluded += 1
            continue
        by_prefix.setdefault(row_prefix, []).append(row)
    matrices: dict[str, Any] = {}
    prefix_ids: dict[str, list[str]] = {}
    for prefix, group in sorted(by_prefix.items()):
        if len(group) < 2:
            continue
        ids = [str(r["id"]) for r in group]
        matrix = cosine_matrix([embeddings[i] for i in ids])
        matrices[prefix] = matrix
        prefix_ids[prefix] = ids
        found = greedy_partition(ids, matrix, threshold=settings.near_dup_threshold)
        degenerate = set(degenerate_cluster_indexes(found, vault_size=len(rows)))
        raw_clusters.extend((cluster, idx in degenerate) for idx, cluster in enumerate(found))

    # Pass 4: merge proposals (distillation) for the near-dup clusters.
    merge_status, cluster_proposals = _build_merge_proposals(
        raw_clusters,
        by_id=by_id,
        settings=settings,
        content_loader=content_loader,
        distiller=distiller,
    )
    clusters.extend(cluster_proposals)

    # Pass 2: age-only staleness (report-only).
    stale_candidates: list[StaleCandidate] = []
    future_dated = 0
    for row in rows:
        created = _row_dt(row, "created_at")
        if is_future_dated(created, now=now):
            future_dated += 1
            continue
        age, anchor = effective_age(
            created_at=created,
            updated_at=_row_dt(row, "updated_at"),
            legacy_created_at=_row_dt_optional(row, "legacy_created_at"),
            now=now,
        )
        if age >= settings.stale_days:
            stale_candidates.append(StaleCandidate(thought=_pin(row), age_days=age, anchor=anchor))
    exclusions = exclusions.model_copy(
        update={"future_dated": future_dated, "prefix_excluded": prefix_excluded}
    )

    # Pass 3: contradiction candidates (LLM-judged, report-only).
    contradiction_status, contradiction_candidates, blocked_count, oversized = (
        _judge_contradictions(
            by_prefix=by_prefix,
            matrices=matrices,
            prefix_ids=prefix_ids,
            by_id=by_id,
            settings=settings,
            content_loader=content_loader,
            judge=judge,
        )
    )
    exclusions = exclusions.model_copy(
        update={
            "block_thoughts_llm": blocked_count,
            "oversized": exclusions.oversized + oversized,
        }
    )

    return ConsolidationReport(
        vault_name=vault_name,
        generated_at=now,
        snapshot_at=now,
        embedding_model=recorded_model or configured_model or "unknown",
        near_dup_threshold=settings.near_dup_threshold,
        contradiction_threshold=settings.contradiction_threshold,
        stale_days=settings.stale_days,
        max_cluster_size=settings.max_cluster_size,
        exclude_prefixes=sorted(excluded_prefixes),
        pass_near_duplicate=PassStatus(
            state=PassState.COMPLETE, done=len(clusters), total=len(clusters)
        ),
        pass_stale=PassStatus(
            state=PassState.COMPLETE, done=len(stale_candidates), total=len(stale_candidates)
        ),
        pass_contradiction=contradiction_status,
        pass_merge=merge_status,
        exclusions=exclusions,
        clusters=clusters,
        stale_candidates=stale_candidates,
        contradiction_candidates=contradiction_candidates,
    )


def _build_merge_proposals(
    raw_clusters: list[tuple[Cluster, bool]],
    *,
    by_id: dict[str, dict[str, Any]],
    settings: ReportSettings,
    content_loader: ContentLoader,
    distiller: DistillFn | None,
) -> tuple[PassStatus, list[ClusterProposal]]:
    """Convert raw similarity clusters into proposals, distilling when eligible."""
    proposals: list[ClusterProposal] = []
    distill_failure: str | None = None
    distilled = 0
    for cluster, degenerate in raw_clusters:
        member_rows = [by_id[i] for i in cluster.member_ids]
        fingerprints = [str(r["fingerprint"]) for r in member_rows]
        portability = most_restrictive_portability([str(r["portability"]) for r in member_rows])
        common = {
            "cluster_id": cluster_id_for(fingerprints),
            "prefix": str(member_rows[0]["prefix"]),
            "members": [_pin(r) for r in member_rows],
            "similarity_floor": max(0.0, cluster.similarity_floor),
            "portability": portability,
        }

        review_reason: str | None = None
        if degenerate:
            review_reason = (
                "cluster spans more than a quarter of the vault; "
                "the threshold is likely mis-set for this corpus"
            )
        elif len(cluster.member_ids) > settings.max_cluster_size:
            review_reason = (
                f"cluster has {len(cluster.member_ids)} members "
                f"(max-cluster-size {settings.max_cluster_size}); review manually"
            )
        elif portability == "block":
            review_reason = "cluster contains a block-portability member; LLM cannot distill it"
        elif distiller is None:
            review_reason = "LLM distillation unavailable (--no-llm or no provider configured)"
        elif distill_failure is not None:
            review_reason = f"distillation stopped earlier in the run: {distill_failure}"

        if review_reason is None and distiller is not None:
            contents = [(i, content_loader(i)) for i in cluster.member_ids]
            total_tokens = sum(estimate_tokens(c) for _, c in contents)
            if total_tokens > settings.max_distill_tokens:
                review_reason = (
                    f"cluster content (~{total_tokens} tokens) exceeds the "
                    f"distillation budget ({settings.max_distill_tokens})"
                )
            else:
                try:
                    draft = distiller(contents, str(common["prefix"]))
                    proposals.append(
                        ClusterProposal(
                            **common,  # type: ignore[arg-type]
                            action=ClusterAction.MERGE,
                            distilled_draft=draft,
                        )
                    )
                    distilled += 1
                    continue
                except (LLMProviderError, BlockThoughtLLMDisallowed) as exc:
                    distill_failure = str(exc)
                    review_reason = f"distillation failed: {exc}"

        proposals.append(
            ClusterProposal(
                **common,  # type: ignore[arg-type]
                action=ClusterAction.MANUAL_REVIEW,
                review_reason=review_reason
                or "LLM distillation unavailable (--no-llm or no provider configured)",
            )
        )

    if distill_failure is not None:
        status = PassStatus(
            state=PassState.INCOMPLETE,
            reason=f"distillation interrupted: {distill_failure}",
            done=distilled,
            total=len(raw_clusters),
        )
    elif distiller is None and raw_clusters:
        status = PassStatus(
            state=PassState.SKIPPED,
            reason="LLM distillation unavailable; clusters emitted as manual-review",
            done=0,
            total=len(raw_clusters),
        )
    else:
        status = PassStatus(state=PassState.COMPLETE, done=distilled, total=len(raw_clusters))
    return status, proposals


def _judge_contradictions(
    *,
    by_prefix: dict[str, list[dict[str, Any]]],
    matrices: dict[str, Any],
    prefix_ids: dict[str, list[str]],
    by_id: dict[str, dict[str, Any]],
    settings: ReportSettings,
    content_loader: ContentLoader,
    judge: JudgeFn | None,
) -> tuple[PassStatus, list[ContradictionCandidate], int, int]:
    """Generate and judge contradiction-band pairs; block thoughts never reach the judge."""
    blocked = {
        str(row["id"])
        for rows in by_prefix.values()
        for row in rows
        if row["portability"] == "block"
    }
    if judge is None:
        status = PassStatus(
            state=PassState.SKIPPED,
            reason="LLM judging unavailable (--no-llm or no provider configured)",
        )
        return status, [], len(blocked), 0

    all_pairs: list[tuple[str, str, float]] = []
    for prefix, matrix in matrices.items():
        ids = prefix_ids[prefix]
        keep_indexes = [k for k, tid in enumerate(ids) if tid not in blocked]
        if len(keep_indexes) < 2:
            continue
        kept_ids = [ids[k] for k in keep_indexes]
        sub_matrix = matrix[keep_indexes][:, keep_indexes]
        all_pairs.extend(
            contradiction_band_pairs(
                kept_ids,
                sub_matrix,
                low=settings.contradiction_threshold,
                high=settings.near_dup_threshold,
            )
        )
    all_pairs.sort(key=lambda p: -p[2])
    total_candidates = len(all_pairs)
    all_pairs = all_pairs[: settings.contradiction_pair_cap]

    candidates: list[ContradictionCandidate] = []
    judged = 0
    oversized = 0
    for first_id, second_id, similarity in all_pairs:
        first_content = content_loader(first_id)
        second_content = content_loader(second_id)
        try:
            verdict, rationale = judge(first_content, second_content)
        except (LLMProviderError, BlockThoughtLLMDisallowed) as exc:
            status = PassStatus(
                state=PassState.INCOMPLETE,
                reason=f"judging interrupted after {judged} of {len(all_pairs)} pairs: {exc}",
                done=judged,
                total=len(all_pairs),
            )
            return status, candidates, len(blocked), oversized
        judged += 1
        if verdict == "oversized":
            oversized += 1
            continue
        if verdict not in ("contradiction", "unclear"):
            continue
        candidates.append(
            ContradictionCandidate(
                first=_pin(by_id[first_id]),
                second=_pin(by_id[second_id]),
                similarity=similarity,
                verdict=ContradictionVerdict(verdict),
                rationale=rationale or "no rationale returned",
            )
        )

    capped = total_candidates > settings.contradiction_pair_cap
    status = PassStatus(
        state=PassState.COMPLETE,
        reason=(
            f"candidate set capped at {settings.contradiction_pair_cap} of {total_candidates}"
            if capped
            else None
        ),
        done=judged,
        total=len(all_pairs),
    )
    return status, candidates, len(blocked), oversized


__all__ = [
    "ContentLoader",
    "DistillFn",
    "JudgeFn",
    "ReportSettings",
    "cluster_id_for",
    "generate_report",
    "most_restrictive_portability",
]
