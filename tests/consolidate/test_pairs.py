"""Tests for engram.consolidate.pairs - contradiction-band pair generation."""

from __future__ import annotations

import numpy as np

from engram.consolidate.clustering import cosine_matrix
from engram.consolidate.pairs import contradiction_band_pairs


def _unit(*coords: float) -> list[float]:
    vec = np.asarray(coords, dtype=np.float64)
    return list(vec / np.linalg.norm(vec))


def test_band_is_inclusive_low_exclusive_high():
    # Pairwise sims (descending): ab > ac > bc.
    matrix = cosine_matrix([_unit(1, 0.2), _unit(1, 0.1), _unit(1, 0.5)])
    low = float(matrix[1, 2])  # exactly the lowest pair (bc)
    high = float(matrix[0, 1])  # exactly the highest pair (ab)
    pairs = contradiction_band_pairs(["a", "b", "c"], matrix, low=low, high=high)
    sims = {(first, second) for first, second, _ in pairs}
    assert ("b", "c") in sims  # == low: included
    assert ("a", "b") not in sims  # == high: excluded (belongs to near-dup pass)
    assert ("a", "c") in sims  # strictly inside the band


def test_sorted_by_similarity_descending():
    matrix = cosine_matrix([_unit(1, 0.2), _unit(1, 0.1), _unit(1, 0.5)])
    pairs = contradiction_band_pairs(["a", "b", "c"], matrix, low=0.0, high=0.999)
    sims = [sim for _, _, sim in pairs]
    assert sims == sorted(sims, reverse=True)


def test_cap_keeps_highest_similarity_pairs():
    matrix = cosine_matrix([_unit(1, 0.2), _unit(1, 0.1), _unit(1, 0.5)])
    pairs = contradiction_band_pairs(["a", "b", "c"], matrix, low=0.0, high=0.999, cap=1)
    assert len(pairs) == 1
    assert {pairs[0][0], pairs[0][1]} == {"a", "b"}  # ab is the highest-similarity pair


def test_no_self_pairs_and_deterministic():
    rng = np.random.default_rng(7)
    matrix = cosine_matrix(rng.normal(size=(10, 4)))
    ids = [f"t{i}" for i in range(10)]
    first = contradiction_band_pairs(ids, matrix, low=0.3, high=0.9)
    second = contradiction_band_pairs(ids, matrix, low=0.3, high=0.9)
    assert first == second
    assert all(a != b for a, b, _ in first)


def test_empty_band_returns_empty():
    matrix = cosine_matrix([_unit(1, 0), _unit(0, 1)])
    assert contradiction_band_pairs(["a", "b"], matrix, low=0.5, high=0.9) == []
