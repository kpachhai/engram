"""End-to-end delete flow at the MCP handler level.

Walks the search-then-delete sequence an AI client would follow:

1. capture_thought captures a thought.
2. search_thoughts finds it.
3. delete_thought(confirm=False) returns a preview, does NOT modify.
4. delete_thought(confirm=True) deletes the thought.
5. fetch returns null thought; search no longer returns it.

The test exercises real :class:`VaultStorage` + a hermetic stub
embedder so the markdown SoT, the SQLite row, and the embedding row
all participate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram.embedding.protocol import EmbeddingProvider
from engram.mcp.tools import (
    capture_thought_handler,
    delete_thought_handler,
    fetch_handler,
    search_thoughts_handler,
)
from engram.models.mcp import (
    CaptureInput,
    DeleteInput,
    FetchInput,
    SearchInput,
)
from engram.storage.facade import VaultStorage

_DIM = 384


class _StubEmbedder:
    """Deterministic stub conforming to EmbeddingProvider."""

    @property
    def model_name(self) -> str:
        return "stub-model"

    @property
    def dimension(self) -> int:
        return _DIM

    def embed(self, text: str) -> list[float]:
        v = [0.0] * _DIM
        # Same vector for the same text so search hits exactly.
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


def test_capture_search_delete_fetch_round_trip(
    vault: VaultStorage, embedder: EmbeddingProvider
) -> None:
    captured = asyncio.run(
        capture_thought_handler(
            vault,
            embedder,
            payload=CaptureInput(content="[Lesson] integration delete flow test"),
        )
    )

    # 1. search finds the thought.
    search_result = asyncio.run(
        search_thoughts_handler(
            vault,
            embedder,
            payload=SearchInput(query="[Lesson] integration delete flow test", k=5),
        )
    )
    assert any(r.id == captured.id for r in search_result.results)

    # 2. dry-run preview.
    preview = asyncio.run(
        delete_thought_handler(vault, payload=DeleteInput(id=captured.id, confirm=False))
    )
    assert preview.deleted is False
    assert preview.body_preview is not None
    # Still present after dry-run.
    assert vault.get_by_id(captured.id) is not None

    # 3. confirmed delete.
    confirmed = asyncio.run(
        delete_thought_handler(vault, payload=DeleteInput(id=captured.id, confirm=True))
    )
    assert confirmed.deleted is True

    # 4. fetch returns null thought (NOT an error, per fetch contract).
    fetched = asyncio.run(fetch_handler(vault, payload=FetchInput(id=captured.id)))
    assert fetched.thought is None

    # 5. search no longer returns the thought.
    after_search = asyncio.run(
        search_thoughts_handler(
            vault,
            embedder,
            payload=SearchInput(query="[Lesson] integration delete flow test", k=5),
        )
    )
    assert all(r.id != captured.id for r in after_search.results)
