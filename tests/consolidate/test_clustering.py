"""Tests for engram.consolidate.clustering - similarity math + greedy partition."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from engram.consolidate.clustering import (
    Cluster,
    cosine_matrix,
    degenerate_cluster_indexes,
    ensure_clusterable_size,
    greedy_partition,
)
from engram.errors import ConsolidateVaultTooLarge


def _unit(*coords: float) -> list[float]:
    vec = np.asarray(coords, dtype=np.float64)
    return list(vec / np.linalg.norm(vec))


class TestCosineMatrix:
    def test_identical_vectors_have_similarity_one(self):
        matrix = cosine_matrix([_unit(1, 0), _unit(1, 0)])
        assert matrix[0, 1] == pytest.approx(1.0)

    def test_orthogonal_vectors_have_similarity_zero(self):
        matrix = cosine_matrix([_unit(1, 0), _unit(0, 1)])
        assert matrix[0, 1] == pytest.approx(0.0)

    def test_zero_vector_yields_zero_similarity_not_nan(self):
        matrix = cosine_matrix([[0.0, 0.0], [1.0, 0.0]])
        assert matrix[0, 1] == 0.0
        assert not np.isnan(matrix).any()

    def test_values_clipped_to_unit_interval(self):
        matrix = cosine_matrix([_unit(1, 1), _unit(1, 1), _unit(1, 0)])
        assert (matrix <= 1.0).all()
        assert (matrix >= -1.0).all()


class TestGreedyPartition:
    def test_pair_above_threshold_clusters(self):
        ids = ["a", "b"]
        matrix = cosine_matrix([_unit(1, 0), _unit(1, 0.05)])
        clusters = greedy_partition(ids, matrix, threshold=0.9)
        assert len(clusters) == 1
        assert set(clusters[0].member_ids) == {"a", "b"}

    def test_identical_vectors_similarity_one_included(self):
        """A strictly-less-than guard must not exclude exact duplicates."""
        ids = ["a", "b"]
        matrix = cosine_matrix([_unit(1, 0), _unit(1, 0)])
        clusters = greedy_partition(ids, matrix, threshold=1.0)
        assert len(clusters) == 1
        assert clusters[0].similarity_floor == pytest.approx(1.0)

    def test_singletons_never_emitted(self):
        ids = ["a", "b", "c"]
        matrix = cosine_matrix([_unit(1, 0), _unit(0, 1), _unit(1, 1)])
        clusters = greedy_partition(ids, matrix, threshold=0.99)
        assert clusters == []

    def test_transitive_chain_does_not_bridge(self):
        """A~B and B~C above threshold but A!~C: complete-linkage admission
        keeps C out instead of chaining the cluster."""
        # Angles 0deg / 30deg / 60deg: cos(30deg) ~= 0.866 between neighbors,
        # cos(60deg) = 0.5 between the endpoints.
        a = _unit(1.0, 0.0)
        b = _unit(np.cos(np.pi / 6), np.sin(np.pi / 6))
        c = _unit(np.cos(np.pi / 3), np.sin(np.pi / 3))
        matrix = cosine_matrix([a, b, c])
        threshold = 0.85
        assert matrix[0, 1] >= threshold
        assert matrix[1, 2] >= threshold
        assert matrix[0, 2] < threshold
        clusters = greedy_partition(["a", "b", "c"], matrix, threshold=threshold)
        assert len(clusters) == 1
        assert set(clusters[0].member_ids) == {"a", "b"}

    def test_similarity_floor_is_min_pairwise(self):
        a = _unit(1.0, 0.0)
        b = _unit(1.0, 0.1)
        c = _unit(1.0, 0.2)
        matrix = cosine_matrix([a, b, c])
        clusters = greedy_partition(["a", "b", "c"], matrix, threshold=0.9)
        assert len(clusters) == 1
        floor = clusters[0].similarity_floor
        expected = min(matrix[0, 1], matrix[0, 2], matrix[1, 2])
        assert floor == pytest.approx(float(expected))

    def test_deterministic_output(self):
        rng = np.random.default_rng(42)
        vectors = rng.normal(size=(20, 8))
        ids = [f"t{i}" for i in range(20)]
        matrix = cosine_matrix(vectors)
        first = greedy_partition(ids, matrix, threshold=0.5)
        second = greedy_partition(ids, matrix, threshold=0.5)
        assert [c.member_ids for c in first] == [c.member_ids for c in second]

    @given(st.integers(min_value=2, max_value=24), st.integers(min_value=1, max_value=2**31))
    def test_partition_property_no_overlap(self, count: int, seed: int):
        rng = np.random.default_rng(seed)
        vectors = rng.normal(size=(count, 4))
        ids = [f"t{i}" for i in range(count)]
        clusters = greedy_partition(ids, cosine_matrix(vectors), threshold=0.8)
        seen: set[str] = set()
        for cluster in clusters:
            assert len(cluster.member_ids) >= 2
            assert cluster.similarity_floor >= 0.8 - 1e-9
            for member in cluster.member_ids:
                assert member not in seen
                seen.add(member)


class TestGuards:
    def test_degenerate_cluster_flagged(self):
        clusters = [
            Cluster(member_ids=("a", "b", "c", "d"), similarity_floor=0.9),
            Cluster(member_ids=("e", "f"), similarity_floor=0.9),
        ]
        flagged = degenerate_cluster_indexes(clusters, vault_size=12, fraction=0.25)
        assert flagged == [0]

    def test_tiny_vault_not_flagged_as_degenerate(self):
        """A 2-of-3 cluster in a tiny vault is normal, not a threshold misfire."""
        clusters = [Cluster(member_ids=("a", "b"), similarity_floor=1.0)]
        assert degenerate_cluster_indexes(clusters, vault_size=3, fraction=0.25) == []

    def test_vault_size_guard_raises(self):
        with pytest.raises(ConsolidateVaultTooLarge, match="20000"):
            ensure_clusterable_size(20001, limit=20000)

    def test_vault_size_guard_passes_at_limit(self):
        ensure_clusterable_size(20000, limit=20000)
