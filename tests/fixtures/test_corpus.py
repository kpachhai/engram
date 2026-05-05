"""Coverage tests for the synthetic fixture corpus generator.

The corpus is consumed by integration tests, property tests, and the
benchmark scripts in ``bench/``. Locking down its invariants here keeps
those downstream surfaces stable.
"""

from __future__ import annotations

from collections import Counter
from itertools import pairwise
from pathlib import Path

import pytest

from engram.embedding.protocol import EmbeddingProvider
from engram.models.frontmatter import CANONICAL_PREFIXES
from engram.storage.facade import VaultStorage
from tests.fixtures.corpus import (
    DEFAULT_CORPUS_SIZE,
    build_corpus,
    write_corpus_to_vault,
)

_DIM = 8


class _StubEmbedder:
    """Tiny deterministic embedder so capture flows exercise the embedding path."""

    @property
    def model_name(self) -> str:
        return "stub"

    @property
    def dimension(self) -> int:
        return _DIM

    def embed(self, text: str) -> list[float]:
        v = [0.0] * _DIM
        v[hash(text) % _DIM] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)


@pytest.fixture
def vault(tmp_path: Path):
    storage = VaultStorage(
        thoughts_dir=tmp_path / "thoughts",
        index_db_path=tmp_path / ".indexes" / "engram.db",
        embedding_dim=_DIM,
    )
    yield storage
    storage.close()


@pytest.fixture
def embedder() -> EmbeddingProvider:
    return _StubEmbedder()


def test_default_corpus_has_expected_size():
    corpus = build_corpus()
    assert len(corpus) == DEFAULT_CORPUS_SIZE


def test_corpus_is_deterministic_for_same_seed():
    a = build_corpus(size=50, seed_label="determinism-check")
    b = build_corpus(size=50, seed_label="determinism-check")
    assert a == b


def test_corpus_changes_with_seed_label():
    a = build_corpus(size=50, seed_label="seed-A")
    b = build_corpus(size=50, seed_label="seed-B")
    assert a != b


def test_corpus_covers_every_canonical_prefix():
    corpus = build_corpus()
    prefixes = {entry.prefix for entry in corpus}
    assert prefixes == set(CANONICAL_PREFIXES)


def test_corpus_covers_every_portability_value():
    corpus = build_corpus()
    counts = Counter(entry.portability for entry in corpus)
    assert counts["portable"] >= 1
    assert counts["sensitive"] >= 1
    assert counts["block"] >= 1


def test_corpus_created_at_is_strictly_increasing():
    corpus = build_corpus()
    timestamps = [entry.created_at for entry in corpus]
    assert all(b > a for a, b in pairwise(timestamps))


def test_corpus_round_trips_through_vault(vault: VaultStorage, embedder):
    corpus = build_corpus(size=20)
    captured = write_corpus_to_vault(vault, corpus, embedder=embedder)
    assert len(captured) == 20

    # Every captured thought must be retrievable by id with content intact.
    # The markdown writer guarantees a trailing newline; the synthetic body
    # does not, so compare after normalizing.
    for thought, source in zip(captured, corpus, strict=True):
        fetched = vault.get_by_id(str(thought.id))
        assert fetched is not None
        assert fetched.content.rstrip("\n") == source.content.rstrip("\n")
        assert fetched.prefix == source.prefix
        assert fetched.portability == source.portability
