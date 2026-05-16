"""Per-file SHA-256 manifest for FastEmbed model integrity verification.

Each FastEmbed model is a collection of files (the ONNX graph, tokenizer
config, vocab, special-tokens map, etc). Engram verifies each file
against its pinned hash before loading.

When pinned hashes are populated:

* A successful match is silent (the model loads).
* A mismatch is FATAL:
  :meth:`engram.embedding.fastembed.FastEmbedProvider.verify_model_files`
  raises :class:`engram.errors.EmbeddingError` and refuses to load.
* A missing file is surfaced by ``engram doctor`` as a WARN in the
  ``embedding_cache_integrity`` check
  (:meth:`engram.embedding.fastembed.FastEmbedProvider.check_cache_integrity`)
  before any embed call is made. The original ``verify_model_files``
  log-warning path remains for the load-time view, but the doctor check
  is the durable surface: a snapshot with the symlinks present but the
  blobs missing causes the embedding load to fail on first search with
  a cryptic ONNX ``NO_SUCHFILE`` rather than a graceful re-download.
  Remediation is to delete the snapshot dir and rerun
  ``engram doctor --download-model``.

When the manifest is empty (e.g. for a model engram has not pinned yet),
``verify_model_files`` logs a single WARNING and proceeds — trust-on-
first-use, relying on HuggingFace HTTPS as the integrity anchor.

Recompute the hashes after a model upgrade via:

    engram doctor --download-model --print-hashes

which prints the manifest in the format below for the maintainer to
paste in.
"""

from __future__ import annotations

from typing import Final

#: SHA-256 hashes for BAAI/bge-small-en-v1.5 model files (the
#: qdrant/bge-small-en-v1.5-onnx-q quantized variant FastEmbed pulls
#: when ``BAAI/bge-small-en-v1.5`` is requested).
#:
#: Recompute via ``engram doctor --download-model --print-hashes`` after
#: any upstream model release.
# Each hash line carries a `pii-allow: hash` marker because the shared
# pii-patterns.conf catches the 40-hex GPG-fingerprint shape, which also
# matches the first 40 chars of any 64-hex SHA-256. Marker placed on the
# hash-bearing line so pii-scan.sh recognises it line-by-line.
BGE_SMALL_EN_V1_5_HASHES: Final[dict[str, str]] = {
    "config.json": (
        "13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd"  # pii-allow: hash
    ),
    "model_optimized.onnx": (
        "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"  # pii-allow: hash
    ),
    "special_tokens_map.json": (
        "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a"  # pii-allow: hash
    ),
    "tokenizer.json": (
        "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"  # pii-allow: hash
    ),
    "tokenizer_config.json": (
        "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53"  # pii-allow: hash
    ),
}

#: Lookup table from model name to its hash manifest.
KNOWN_MODEL_HASHES: Final[dict[str, dict[str, str]]] = {
    "BAAI/bge-small-en-v1.5": BGE_SMALL_EN_V1_5_HASHES,
}


__all__ = [
    "BGE_SMALL_EN_V1_5_HASHES",
    "KNOWN_MODEL_HASHES",
]
