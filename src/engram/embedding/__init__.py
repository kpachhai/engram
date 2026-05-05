"""Embedding layer for engram.

The :class:`EmbeddingProvider` protocol is the abstraction the storage layer
uses for vector generation. :class:`FastEmbedProvider` is the default
implementation backed by FastEmbed (``BAAI/bge-small-en-v1.5`` by default,
384-dim).

Per-file SHA-256 model verification is structured but currently ships with
empty hash manifests. ``engram doctor --download-model`` prints the
computed hashes for the maintainer to pin into ``model_hashes.py``. Until
real hashes are populated, the FastEmbed default trust anchor
(HuggingFace HTTPS) is the integrity boundary.
"""

from __future__ import annotations

from engram.embedding.fastembed import FastEmbedProvider
from engram.embedding.model_hashes import (
    BGE_SMALL_EN_V1_5_HASHES,
    KNOWN_MODEL_HASHES,
)
from engram.embedding.protocol import EmbeddingProvider

__all__ = [
    "BGE_SMALL_EN_V1_5_HASHES",
    "KNOWN_MODEL_HASHES",
    "EmbeddingProvider",
    "FastEmbedProvider",
]
