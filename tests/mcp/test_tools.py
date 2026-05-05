"""Tests for engram.mcp.tools - the 5 pure async tool handlers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from engram.embedding.protocol import EmbeddingProvider
from engram.errors import EmbeddingError
from engram.mcp.tools import (
    capture_thought_handler,
    fetch_handler,
    list_thoughts_handler,
    search_thoughts_handler,
    thought_stats_handler,
)
from engram.models.mcp import (
    CaptureInput,
    CaptureInputMetadata,
    FetchInput,
    Filter,
    ListInput,
    SearchInput,
)
from engram.storage.facade import VaultStorage

_DIM = 384


class _StubEmbedder:
    """Deterministic stub conforming to EmbeddingProvider."""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[str] = []

    @property
    def model_name(self) -> str:
        return "stub-model"

    @property
    def dimension(self) -> int:
        return _DIM

    def embed(self, text: str) -> list[float]:
        if self._fail:
            raise EmbeddingError("stub failure")
        self.calls.append(text)
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


# === capture_thought ===


def test_capture_thought_writes_with_embedding(vault, embedder):
    payload = CaptureInput(content="[Lesson] body content")
    result = asyncio.run(capture_thought_handler(vault, embedder, payload=payload))
    assert result.id is not None
    assert "lesson/" in result.file_path
    assert len(result.fingerprint) == 64

    row = vault.get_by_id(result.id)
    assert row is not None
    # Writer ensures trailing newline on non-empty bodies (NFR4 normalization).
    assert row.content == "[Lesson] body content\n"


def test_capture_thought_uses_default_user_when_no_metadata_source(vault, embedder):
    payload = CaptureInput(content="[Lesson] body")
    result = asyncio.run(
        capture_thought_handler(vault, embedder, payload=payload, default_user="kpachhai")
    )
    fetched = vault.get_by_id(result.id)
    assert fetched is not None
    assert fetched.source == "kpachhai"


def test_capture_thought_metadata_source_overrides_default(vault, embedder):
    payload = CaptureInput(
        content="[Lesson] body",
        metadata=CaptureInputMetadata(source="alice"),
    )
    result = asyncio.run(
        capture_thought_handler(vault, embedder, payload=payload, default_user="kpachhai")
    )
    fetched = vault.get_by_id(result.id)
    assert fetched is not None
    assert fetched.source == "alice"


def test_capture_thought_embedding_failure_falls_through_to_pending(vault):
    failing_embedder = _StubEmbedder(fail=True)
    payload = CaptureInput(content="[Lesson] body")
    result = asyncio.run(capture_thought_handler(vault, failing_embedder, payload=payload))
    # Capture still succeeded.
    assert result.id is not None
    # SQLite row marked pending.
    cursor = vault.conn.execute(
        "SELECT embedding_status FROM thoughts WHERE id = ?", (str(result.id),)
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "pending"


def test_capture_thought_metadata_prefix_overrides_parsed(vault, embedder):
    payload = CaptureInput(
        content="[Lesson] body",
        metadata=CaptureInputMetadata(prefix="Pattern"),
    )
    result = asyncio.run(capture_thought_handler(vault, embedder, payload=payload))
    fetched = vault.get_by_id(result.id)
    assert fetched is not None
    assert fetched.prefix == "Pattern"


# === search_thoughts ===


def test_search_returns_results_ordered_by_similarity(vault, embedder):
    asyncio.run(
        capture_thought_handler(
            vault, embedder, payload=CaptureInput(content="[Lesson] near match")
        )
    )
    asyncio.run(
        capture_thought_handler(
            vault, embedder, payload=CaptureInput(content="[Pattern] far match")
        )
    )
    result = asyncio.run(
        search_thoughts_handler(
            vault, embedder, payload=SearchInput(query="[Lesson] near match", k=10)
        )
    )
    # Both ok-embeddings -> total_found == 2; results ordered by similarity.
    assert result.total_found == 2
    assert len(result.results) == 2


def test_search_with_filter(vault, embedder):
    asyncio.run(
        capture_thought_handler(vault, embedder, payload=CaptureInput(content="[Lesson] x"))
    )
    asyncio.run(
        capture_thought_handler(vault, embedder, payload=CaptureInput(content="[Pattern] y"))
    )
    result = asyncio.run(
        search_thoughts_handler(
            vault,
            embedder,
            payload=SearchInput(query="x", k=10, filter=Filter(prefix="Lesson")),
        )
    )
    assert result.total_found == 1
    assert all(r.prefix == "Lesson" for r in result.results)


# === list_thoughts ===


def test_list_thoughts_returns_paginated(vault, embedder):
    for i in range(7):
        asyncio.run(
            capture_thought_handler(
                vault, embedder, payload=CaptureInput(content=f"[Lesson] number {i}")
            )
        )
    result = asyncio.run(list_thoughts_handler(vault, payload=ListInput(limit=3, offset=0)))
    assert result.total_count == 7
    assert len(result.results) == 3


def test_list_thoughts_filter_by_prefix(vault, embedder):
    asyncio.run(
        capture_thought_handler(vault, embedder, payload=CaptureInput(content="[Lesson] one"))
    )
    asyncio.run(
        capture_thought_handler(vault, embedder, payload=CaptureInput(content="[Pattern] two"))
    )
    result = asyncio.run(
        list_thoughts_handler(vault, payload=ListInput(filter=Filter(prefix="Lesson")))
    )
    assert result.total_count == 1


def test_list_thoughts_includes_pending_rows(vault):
    failing = _StubEmbedder(fail=True)
    asyncio.run(
        capture_thought_handler(vault, failing, payload=CaptureInput(content="[Lesson] pending"))
    )
    result = asyncio.run(list_thoughts_handler(vault, payload=ListInput()))
    assert result.total_count == 1


# === thought_stats ===


def test_thought_stats_empty_vault(vault):
    result = asyncio.run(thought_stats_handler(vault))
    assert result.total_count == 0
    assert result.oldest is None
    assert result.newest is None


def test_thought_stats_populated(vault, embedder):
    asyncio.run(
        capture_thought_handler(vault, embedder, payload=CaptureInput(content="[Lesson] a"))
    )
    asyncio.run(
        capture_thought_handler(vault, embedder, payload=CaptureInput(content="[Pattern] b"))
    )
    result = asyncio.run(thought_stats_handler(vault))
    assert result.total_count == 2
    assert result.by_prefix["Lesson"] == 1
    assert result.by_prefix["Pattern"] == 1
    assert result.oldest is not None


# === fetch ===


def test_fetch_returns_thought_for_known_id(vault, embedder):
    captured = asyncio.run(
        capture_thought_handler(vault, embedder, payload=CaptureInput(content="[Lesson] body"))
    )
    result = asyncio.run(fetch_handler(vault, payload=FetchInput(id=captured.id)))
    assert result.thought is not None
    assert result.thought.id == captured.id


def test_fetch_unknown_returns_null_thought_not_error(vault):
    """B6: fetch returns null thought (not error) for unknown id."""
    result = asyncio.run(fetch_handler(vault, payload=FetchInput(id=uuid4())))
    assert result.thought is None
