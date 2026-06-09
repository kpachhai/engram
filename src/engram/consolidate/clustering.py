"""Similarity math and greedy partition clustering for the near-duplicate pass.

The full pairwise cosine matrix is computed in numpy (single-query KNN would
truncate large clusters). Clustering is a greedy highest-similarity-first
partition with complete-linkage admission: a thought joins a cluster only if
its similarity to EVERY current member meets the threshold, so transitive
chains (A~B, B~C, A!~C) never bridge into one merge. Each thought belongs to
at most one cluster by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from engram.errors import ConsolidateVaultTooLarge

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import ArrayLike, NDArray

#: Refuse the O(n^2) similarity pass beyond this many embedded thoughts.
DEFAULT_VAULT_SIZE_LIMIT = 20_000

#: A single cluster spanning more than this fraction of the vault signals a
#: mis-set threshold, not a merge opportunity.
DEGENERATE_CLUSTER_FRACTION = 0.25

#: Below this vault size, large relative clusters are normal and the
#: degenerate guard stays quiet.
_DEGENERATE_GUARD_MIN_VAULT = 8


@dataclass(frozen=True)
class Cluster:
    """A near-duplicate cluster: member ids + the minimum pairwise similarity."""

    member_ids: tuple[str, ...]
    similarity_floor: float


def cosine_matrix(vectors: ArrayLike) -> NDArray[np.float64]:
    """Pairwise cosine similarity, clipped to [-1, 1]; zero vectors yield 0."""
    array = np.asarray(vectors, dtype=np.float64)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    safe_norms = np.where(norms == 0.0, 1.0, norms)
    normalized = array / safe_norms
    matrix: NDArray[np.float64] = np.clip(normalized @ normalized.T, -1.0, 1.0)
    return matrix


def ensure_clusterable_size(count: int, *, limit: int = DEFAULT_VAULT_SIZE_LIMIT) -> None:
    """Refuse the pairwise pass when the embedded-thought count exceeds ``limit``."""
    if count > limit:
        msg = (
            f"vault has {count} embedded thoughts; the similarity pass supports "
            f"at most {limit}. Consolidate per prefix (--prefix) to reduce scope."
        )
        raise ConsolidateVaultTooLarge(msg)


def greedy_partition(
    ids: Sequence[str],
    matrix: NDArray[np.float64],
    *,
    threshold: float,
) -> list[Cluster]:
    """Partition ``ids`` into near-duplicate clusters.

    Pairs at/above ``threshold`` are visited in descending-similarity order
    (deterministic index tiebreak). A pair of unassigned thoughts founds a
    cluster; an unassigned thought joins an existing member's cluster only
    under complete-linkage admission. Singletons are never emitted.
    """
    count = len(ids)
    if count < 2:
        return []

    upper_i, upper_j = np.triu_indices(count, k=1)
    sims = matrix[upper_i, upper_j]
    eligible = sims >= threshold
    candidate_sims = sims[eligible]
    candidate_pairs = np.column_stack((upper_i[eligible], upper_j[eligible]))
    # Descending similarity; ties broken by (i, j) so output is deterministic.
    order = np.lexsort((candidate_pairs[:, 1], candidate_pairs[:, 0], -candidate_sims))

    assignment: dict[int, int] = {}
    clusters: list[list[int]] = []
    for index in order:
        first, second = int(candidate_pairs[index, 0]), int(candidate_pairs[index, 1])
        first_cluster = assignment.get(first)
        second_cluster = assignment.get(second)
        if first_cluster is None and second_cluster is None:
            assignment[first] = assignment[second] = len(clusters)
            clusters.append([first, second])
        elif first_cluster is not None and second_cluster is None:
            _try_admit(
                second, clusters[first_cluster], matrix, threshold, assignment, first_cluster
            )
        elif first_cluster is None and second_cluster is not None:
            _try_admit(
                first, clusters[second_cluster], matrix, threshold, assignment, second_cluster
            )
        # Both already assigned: clusters are never merged.

    result: list[Cluster] = []
    for members in clusters:
        pair_rows = np.ix_(members, members)
        pairwise = matrix[pair_rows]
        floor = float(pairwise[np.triu_indices(len(members), k=1)].min())
        result.append(Cluster(member_ids=tuple(ids[m] for m in members), similarity_floor=floor))
    return result


def _try_admit(
    candidate: int,
    members: list[int],
    matrix: NDArray[np.float64],
    threshold: float,
    assignment: dict[int, int],
    cluster_index: int,
) -> None:
    """Admit ``candidate`` only if it meets the threshold against every member."""
    if all(matrix[candidate, member] >= threshold for member in members):
        members.append(candidate)
        assignment[candidate] = cluster_index


def degenerate_cluster_indexes(
    clusters: Sequence[Cluster],
    *,
    vault_size: int,
    fraction: float = DEGENERATE_CLUSTER_FRACTION,
) -> list[int]:
    """Indexes of clusters so large they indicate a mis-set threshold."""
    if vault_size < _DEGENERATE_GUARD_MIN_VAULT:
        return []
    return [
        index
        for index, cluster in enumerate(clusters)
        if len(cluster.member_ids) > vault_size * fraction
    ]


__all__ = [
    "DEFAULT_VAULT_SIZE_LIMIT",
    "DEGENERATE_CLUSTER_FRACTION",
    "Cluster",
    "cosine_matrix",
    "degenerate_cluster_indexes",
    "ensure_clusterable_size",
    "greedy_partition",
]
