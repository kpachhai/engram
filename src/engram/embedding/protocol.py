"""Protocol definition for embedding providers.

The storage layer + MCP server depend only on this Protocol so test harnesses
can inject a deterministic stub in place of FastEmbed without depending on
the network or onnxruntime.
"""

from __future__ import annotations

from typing import Protocol


class EmbeddingProvider(Protocol):
    """Structural type for engram embedding providers."""

    @property
    def model_name(self) -> str:
        """Return the embedding model identifier (e.g. ``"BAAI/bge-small-en-v1.5"``)."""
        ...

    @property
    def dimension(self) -> int:
        """Return the vector dimension of this provider's embeddings."""
        ...

    def embed(self, text: str) -> list[float]:
        """Synchronously generate an embedding for ``text``."""
        ...

    async def aembed(self, text: str) -> list[float]:
        """Async wrapper around :meth:`embed`; suitable for asyncio call sites."""
        ...


__all__ = ["EmbeddingProvider"]
