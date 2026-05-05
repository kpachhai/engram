"""Per-file SHA-256 manifest for FastEmbed model integrity verification.

Per ``06-SECURITY.md`` "FastEmbed Model Integrity": each FastEmbed model is a
collection of files (``model.onnx``, ``tokenizer.json``, ``config.json``,
sometimes ``model.onnx_data``). Engram verifies each file against its pinned
hash before loading.

Phase 1 ships with empty manifests; populating the real hashes is a Phase 1.1
follow-up so this is not yet a security boundary. Until populated:

* :meth:`engram.embedding.fastembed.FastEmbedProvider.verify_model_files` logs
  a WARNING and proceeds (trust-on-first-use; relies on HuggingFace HTTPS as
  the integrity anchor).
* ``engram doctor --download-model --print-hashes`` (Phase 1.1) computes the
  current hashes and prints them in this file's format so the maintainer can
  pin them at release time.

When populated, mismatches are FATAL: the provider raises
:class:`engram.errors.EmbeddingError` and refuses to load.
"""

from __future__ import annotations

from typing import Final

#: SHA-256 hashes for BAAI/bge-small-en-v1.5 model files. Empty in Phase 1.
#: Populate at release time via ``engram doctor --download-model --print-hashes``.
BGE_SMALL_EN_V1_5_HASHES: Final[dict[str, str]] = {
    # "model.onnx": "<sha256-hex>",
    # "tokenizer.json": "<sha256-hex>",
    # "config.json": "<sha256-hex>",
    # "special_tokens_map.json": "<sha256-hex>",
    # "tokenizer_config.json": "<sha256-hex>",
    # "vocab.txt": "<sha256-hex>",
}

#: Lookup table from model name to its hash manifest.
KNOWN_MODEL_HASHES: Final[dict[str, dict[str, str]]] = {
    "BAAI/bge-small-en-v1.5": BGE_SMALL_EN_V1_5_HASHES,
}


__all__ = [
    "BGE_SMALL_EN_V1_5_HASHES",
    "KNOWN_MODEL_HASHES",
]
