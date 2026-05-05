"""Property-based tests for engram storage and embedding invariants."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engram.embedding.protocol import EmbeddingProvider
from engram.storage.facade import VaultStorage
from engram.storage.reindex import ReindexMode, reindex_vault
from engram.utils.fingerprint import compute_fingerprint, normalize_body

_DIM = 8


class _StubEmbedder:
    """Deterministic 8-dim embedder for property tests."""

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


# Body strategy: printable text plus newlines/tabs, 1-200 chars, never empty
# after stripping (capture rejects whitespace-only content).
_body_strategy = st.text(
    alphabet=st.characters(
        min_codepoint=0x20,
        max_codepoint=0x7E,
        whitelist_characters="\n\t",
    ),
    min_size=1,
    max_size=200,
).filter(lambda s: bool(s.strip()))


# === Invariant 1: capture -> fetch returns the same content. ===


@given(body=_body_strategy)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_capture_then_fetch_round_trips(vault: VaultStorage, body: str) -> None:
    """For any non-blank body, capturing then fetching by id returns equivalent content."""
    thought = vault.capture(content=body, prefix="Lesson", source="prop-tester")
    fetched = vault.get_by_id(str(thought.id))
    assert fetched is not None
    # The markdown writer normalizes line endings and ensures a trailing newline;
    # compare against the same normalization the storage layer applies.
    expected = body.replace("\r\n", "\n").replace("\r", "\n")
    if expected and not expected.endswith("\n"):
        expected = expected + "\n"
    assert fetched.content == expected


# === Invariant 2: fingerprint stable across whitespace-equivalent inputs. ===


@given(
    body=st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        min_size=1,
        max_size=120,
    ).filter(lambda s: s.strip() and "\n" not in s),
    trailing_blanks=st.integers(min_value=0, max_value=5),
    trailing_spaces=st.integers(min_value=0, max_value=5),
)
def test_fingerprint_is_stable_under_trivial_whitespace(
    body: str,
    trailing_blanks: int,
    trailing_spaces: int,
) -> None:
    """Trailing per-line whitespace + trailing blank lines do not affect the fingerprint."""
    augmented = body + (" " * trailing_spaces) + ("\n" * trailing_blanks)
    assert compute_fingerprint(body) == compute_fingerprint(augmented)


@given(
    body=st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        min_size=1,
        max_size=120,
    ).filter(lambda s: s.strip() and "\n" not in s),
)
def test_fingerprint_is_stable_under_line_ending_translation(body: str) -> None:
    """The same logical body in LF, CRLF, and CR should produce one fingerprint."""
    multi_line = body + "\nsecond line\nthird line"
    lf = multi_line
    crlf = multi_line.replace("\n", "\r\n")
    cr = multi_line.replace("\n", "\r")
    assert compute_fingerprint(lf) == compute_fingerprint(crlf) == compute_fingerprint(cr)


@given(
    body=st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        min_size=1,
        max_size=200,
    ).filter(lambda s: bool(s.strip())),
)
def test_normalize_body_is_idempotent(body: str) -> None:
    """Normalizing the body twice equals normalizing once."""
    once = normalize_body(body)
    twice = normalize_body(once.decode("utf-8"))
    assert once == twice


# === Invariant 3: search returns at most k results. ===


@given(
    bodies=st.lists(_body_strategy, min_size=1, max_size=8, unique=True),
    k=st.integers(min_value=1, max_value=20),
)
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_search_returns_at_most_k_results(
    vault: VaultStorage,
    embedder: EmbeddingProvider,
    bodies: list[str],
    k: int,
) -> None:
    """For any captured corpus and any k, search returns at most k results."""
    for body in bodies:
        embedding = embedder.embed(body)
        vault.capture(
            content=body,
            prefix="Lesson",
            source="prop-tester",
            embedding=embedding,
        )

    query = embedder.embed(bodies[0])
    results, _ = vault.search(query_embedding=query, k=k)
    assert len(results) <= k


# === Invariant 4: incremental reindex is idempotent. ===


@given(bodies=st.lists(_body_strategy, min_size=1, max_size=6, unique=True))
@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_reindex_is_idempotent(
    vault: VaultStorage,
    embedder: EmbeddingProvider,
    bodies: list[str],
) -> None:
    """A second incremental reindex over an already-indexed vault re-embeds nothing."""
    for body in bodies:
        embedding = embedder.embed(body)
        vault.capture(
            content=body,
            prefix="Lesson",
            source="prop-tester",
            embedding=embedding,
        )

    first = reindex_vault(
        vault,
        mode=ReindexMode.INCREMENTAL,
        embed_fn=embedder.embed,
    )
    # The second pass must be a no-op: every file is already indexed and the
    # canonical fingerprints already match.
    second = reindex_vault(
        vault,
        mode=ReindexMode.INCREMENTAL,
        embed_fn=embedder.embed,
    )
    assert second.walked == first.walked
    assert second.inserted == 0
    assert second.body_reindexed == 0
    assert second.metadata_reindexed == 0
    assert second.embeddings_repaired == 0
    assert second.drift_observations == []
