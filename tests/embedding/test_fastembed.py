"""Tests for engram.embedding.fastembed.

Most tests use a mocked FastEmbed to avoid the ~130MB model download. An
``integration``-marked test exercises the real FastEmbed model load and
embed; CI runs it but local fast-feedback runs skip it.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Iterable
from pathlib import Path

import pytest

from engram.embedding.fastembed import (
    DEFAULT_DIMENSION,
    DEFAULT_MODEL_NAME,
    FastEmbedProvider,
    _sha256_of_file,
)
from engram.errors import EmbeddingError


class _FakeFastEmbed:
    """Stand-in for fastembed.TextEmbedding that returns deterministic vectors."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def embed(self, texts: list[str]) -> Iterable[list[float]]:
        for text in texts:
            v = [0.0] * DEFAULT_DIMENSION
            v[0] = float(len(text)) / 100.0
            yield v


class _MismatchedDimFakeFastEmbed:
    """Returns 256-dim vectors so dimension verification fails."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def embed(self, texts: list[str]) -> Iterable[list[float]]:
        for _ in texts:
            yield [0.0] * 256


@pytest.fixture
def fake_fastembed_module(monkeypatch: pytest.MonkeyPatch):
    """Inject a fake fastembed module into sys.modules to avoid real downloads."""
    module = types.ModuleType("fastembed")
    module.TextEmbedding = _FakeFastEmbed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return module


@pytest.fixture
def fake_fastembed_dim_mismatch(monkeypatch: pytest.MonkeyPatch):
    module = types.ModuleType("fastembed")
    module.TextEmbedding = _MismatchedDimFakeFastEmbed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return module


# === construction is lazy ===


def test_init_does_not_load_model(fake_fastembed_module):
    del fake_fastembed_module
    provider = FastEmbedProvider()
    assert provider.is_loaded is False
    assert provider.model_name == DEFAULT_MODEL_NAME
    assert provider.dimension == DEFAULT_DIMENSION


def test_first_embed_call_triggers_load(fake_fastembed_module):
    del fake_fastembed_module
    provider = FastEmbedProvider()
    assert provider.is_loaded is False
    vec = provider.embed("hello")
    assert provider.is_loaded is True
    assert len(vec) == DEFAULT_DIMENSION


def test_repeated_embed_calls_reuse_loaded_model(fake_fastembed_module):
    del fake_fastembed_module
    provider = FastEmbedProvider()
    provider.embed("first")
    model_obj_id = id(provider._model)
    provider.embed("second")
    assert id(provider._model) == model_obj_id


# === embed correctness ===


def test_embed_returns_correct_dimension(fake_fastembed_module):
    del fake_fastembed_module
    provider = FastEmbedProvider()
    vec = provider.embed("hello world")
    assert len(vec) == DEFAULT_DIMENSION


def test_embed_returns_floats(fake_fastembed_module):
    del fake_fastembed_module
    provider = FastEmbedProvider()
    vec = provider.embed("hello")
    assert all(isinstance(x, float) for x in vec)


def test_async_embed_works(fake_fastembed_module):
    del fake_fastembed_module
    provider = FastEmbedProvider()
    result = asyncio.run(provider.aembed("async input"))
    assert len(result) == DEFAULT_DIMENSION


# === dimension mismatch is fatal ===


def test_dimension_mismatch_raises(fake_fastembed_dim_mismatch):
    del fake_fastembed_dim_mismatch
    provider = FastEmbedProvider()
    with pytest.raises(EmbeddingError, match="produced 256-dim"):
        provider.embed("test")


# === hash verification ===


def test_verify_model_files_with_empty_manifest_warns_only(
    fake_fastembed_module, caplog: pytest.LogCaptureFixture
):
    del fake_fastembed_module
    provider = FastEmbedProvider()
    with caplog.at_level("WARNING", logger="engram.embedding.fastembed"):
        provider.verify_model_files()
    assert any("no pinned SHA-256 manifest" in rec.message for rec in caplog.records)


def test_verify_model_files_with_populated_manifest_detects_mismatch(
    tmp_path: Path,
    fake_fastembed_module,
    monkeypatch: pytest.MonkeyPatch,
):
    """When the manifest is populated, mismatched files raise EmbeddingError."""
    del fake_fastembed_module

    cache_dir = tmp_path / "fastembed-cache"
    cache_dir.mkdir()
    (cache_dir / "model.onnx").write_bytes(b"actual content of model")

    fake_manifest = {"BAAI/bge-small-en-v1.5": {"model.onnx": "f" * 64}}
    monkeypatch.setattr("engram.embedding.fastembed.KNOWN_MODEL_HASHES", fake_manifest)

    provider = FastEmbedProvider(cache_dir=cache_dir)
    with pytest.raises(EmbeddingError, match="model integrity check failed"):
        provider.verify_model_files()


def test_verify_model_files_with_populated_manifest_accepts_match(
    tmp_path: Path,
    fake_fastembed_module,
    monkeypatch: pytest.MonkeyPatch,
):
    del fake_fastembed_module
    cache_dir = tmp_path / "fastembed-cache"
    cache_dir.mkdir()
    content = b"actual content of model file"
    (cache_dir / "model.onnx").write_bytes(content)
    real_hash = _sha256_of_file(cache_dir / "model.onnx")

    fake_manifest = {"BAAI/bge-small-en-v1.5": {"model.onnx": real_hash}}
    monkeypatch.setattr("engram.embedding.fastembed.KNOWN_MODEL_HASHES", fake_manifest)

    provider = FastEmbedProvider(cache_dir=cache_dir)
    provider.verify_model_files()  # no raise


def test_verify_model_files_missing_file_warns_only(
    tmp_path: Path,
    fake_fastembed_module,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    del fake_fastembed_module
    cache_dir = tmp_path / "fastembed-cache"
    cache_dir.mkdir()
    fake_manifest = {"BAAI/bge-small-en-v1.5": {"model.onnx": "0" * 64}}
    monkeypatch.setattr("engram.embedding.fastembed.KNOWN_MODEL_HASHES", fake_manifest)

    provider = FastEmbedProvider(cache_dir=cache_dir)
    with caplog.at_level("WARNING", logger="engram.embedding.fastembed"):
        provider.verify_model_files()
    assert any("missing in" in rec.message for rec in caplog.records)


# === sha256 helper ===


def test_sha256_of_file(tmp_path: Path):
    """SHA-256 stream-hashing for hash-pinning verification."""
    file_path = tmp_path / "blob.bin"
    file_path.write_bytes(b"hello")
    digest = _sha256_of_file(file_path)
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_sha256_of_file_handles_large_files(tmp_path: Path):
    """Stream-hash should handle files larger than the chunk size."""
    file_path = tmp_path / "big.bin"
    file_path.write_bytes(b"a" * (100 * 1024))  # 100 KB
    digest = _sha256_of_file(file_path)
    assert len(digest) == 64
    # Compute expected digest manually for cross-check.
    import hashlib

    expected = hashlib.sha256(b"a" * (100 * 1024)).hexdigest()
    assert digest == expected


# === integration test: real FastEmbed download ===


@pytest.mark.integration
@pytest.mark.slow
def test_real_fastembed_round_trip():
    """Smoke test: real FastEmbed model loads and produces 384-dim vectors.

    Requires network access on first run (model download).
    """
    pytest.importorskip("fastembed")
    provider = FastEmbedProvider()
    vec = provider.embed("integration test sentence")
    assert len(vec) == DEFAULT_DIMENSION
    assert provider.is_loaded
    # Vector should be normalized (or at least bounded) for bge-small.
    norm_sq = sum(x * x for x in vec)
    assert 0.0 < norm_sq < 100.0
