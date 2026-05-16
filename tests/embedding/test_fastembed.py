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
    loaded_before = provider.is_loaded
    vec = provider.embed("hello")
    loaded_after = provider.is_loaded
    assert loaded_before is False
    assert loaded_after is True
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


def _make_hf_snapshot_dir(cache_dir: Path) -> Path:
    """Create a fake HuggingFace cache layout under ``cache_dir``.

    Returns the snapshot directory inside which test files should be
    written. Layout: ``<cache>/models--qdrant--bge-small-en-v1.5-onnx-q/snapshots/<sha>/``.
    """
    snapshot = cache_dir / "models--test--repo" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    return snapshot


def test_verify_model_files_with_empty_manifest_warns_only(
    fake_fastembed_module,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """When KNOWN_MODEL_HASHES has no entry for the model, verify warns + returns."""
    del fake_fastembed_module
    # Override the module-level constant to simulate an unpinned model.
    monkeypatch.setattr("engram.embedding.fastembed.KNOWN_MODEL_HASHES", {})
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
    snapshot = _make_hf_snapshot_dir(cache_dir)
    (snapshot / "model.onnx").write_bytes(b"actual content of model")

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
    snapshot = _make_hf_snapshot_dir(cache_dir)
    content = b"actual content of model file"
    (snapshot / "model.onnx").write_bytes(content)
    real_hash = _sha256_of_file(snapshot / "model.onnx")

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
    _make_hf_snapshot_dir(cache_dir)  # snapshot exists but no model.onnx in it
    fake_manifest = {"BAAI/bge-small-en-v1.5": {"model.onnx": "0" * 64}}
    monkeypatch.setattr("engram.embedding.fastembed.KNOWN_MODEL_HASHES", fake_manifest)

    provider = FastEmbedProvider(cache_dir=cache_dir)
    with caplog.at_level("WARNING", logger="engram.embedding.fastembed"):
        provider.verify_model_files()
    assert any("missing in" in rec.message for rec in caplog.records)


def test_list_cached_files_returns_snapshot_files(tmp_path: Path):
    """list_cached_files surfaces the snapshot dir's files (used by --print-hashes)."""
    cache_dir = tmp_path / "fastembed-cache"
    cache_dir.mkdir()
    snapshot = _make_hf_snapshot_dir(cache_dir)
    (snapshot / "model.onnx").write_bytes(b"x")
    (snapshot / "tokenizer.json").write_bytes(b"y")
    provider = FastEmbedProvider(cache_dir=cache_dir)
    files = provider.list_cached_files()
    assert "model.onnx" in files
    assert "tokenizer.json" in files


def test_list_cached_files_returns_empty_when_no_snapshot(tmp_path: Path):
    cache_dir = tmp_path / "empty-cache"
    cache_dir.mkdir()
    provider = FastEmbedProvider(cache_dir=cache_dir)
    assert provider.list_cached_files() == {}


# === cache integrity (read-only, never triggers download) ===


def test_check_cache_integrity_reports_no_cache_when_dir_missing(tmp_path: Path):
    """No cache root on disk -> ``cache_dir`` is None in the report."""
    cache_dir = tmp_path / "never-created"
    provider = FastEmbedProvider(cache_dir=cache_dir)
    report = provider.check_cache_integrity()
    assert report.cache_dir is None
    assert report.snapshot_dir is None
    assert report.missing_files == ()
    assert report.is_intact is False
    assert report.has_snapshot is False


def test_check_cache_integrity_reports_no_snapshot_when_cache_empty(tmp_path: Path):
    """Cache root exists but no snapshots yet -> snapshot_dir None, no missing."""
    cache_dir = tmp_path / "empty-cache"
    cache_dir.mkdir()
    provider = FastEmbedProvider(cache_dir=cache_dir)
    report = provider.check_cache_integrity()
    assert report.cache_dir == cache_dir
    assert report.snapshot_dir is None
    assert report.missing_files == ()


def test_check_cache_integrity_reports_intact_when_all_files_present(tmp_path: Path):
    """Snapshot dir with every manifest file present -> is_intact True, no missing."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    snapshot = _make_hf_snapshot_dir(cache_dir)
    from engram.embedding.model_hashes import BGE_SMALL_EN_V1_5_HASHES

    for filename in BGE_SMALL_EN_V1_5_HASHES:
        (snapshot / filename).write_bytes(b"stub")

    provider = FastEmbedProvider(cache_dir=cache_dir)
    report = provider.check_cache_integrity()
    assert report.manifest_populated is True
    assert report.snapshot_dir == snapshot
    assert report.missing_files == ()
    assert report.is_intact is True


def test_check_cache_integrity_reports_missing_files_when_snapshot_partial(tmp_path: Path):
    """Partial snapshot (some files absent) -> missing_files lists the gaps."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    snapshot = _make_hf_snapshot_dir(cache_dir)
    from engram.embedding.model_hashes import BGE_SMALL_EN_V1_5_HASHES

    # Write everything EXCEPT model_optimized.onnx - the exact failure shape
    # that triggers ONNX NO_SUCHFILE at first embed call.
    for filename in BGE_SMALL_EN_V1_5_HASHES:
        if filename == "model_optimized.onnx":
            continue
        (snapshot / filename).write_bytes(b"stub")

    provider = FastEmbedProvider(cache_dir=cache_dir)
    report = provider.check_cache_integrity()
    assert "model_optimized.onnx" in report.missing_files
    assert report.is_intact is False


def test_check_cache_integrity_detects_dangling_symlink(tmp_path: Path):
    """A symlink whose target blob is gone counts as missing.

    HuggingFace's cache layout stores file contents as blobs and the
    snapshot dir as symlinks into ``blobs/``. When the blob is deleted
    but the snapshot symlink remains, ``Path.exists`` returns False
    on the dangling link and we count it as missing.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    snapshot = _make_hf_snapshot_dir(cache_dir)
    from engram.embedding.model_hashes import BGE_SMALL_EN_V1_5_HASHES

    for filename in BGE_SMALL_EN_V1_5_HASHES:
        if filename == "model_optimized.onnx":
            continue
        (snapshot / filename).write_bytes(b"stub")

    # Add a dangling symlink for model_optimized.onnx.
    blob = tmp_path / "blob_will_be_deleted"
    blob.write_bytes(b"placeholder")
    (snapshot / "model_optimized.onnx").symlink_to(blob)
    blob.unlink()

    provider = FastEmbedProvider(cache_dir=cache_dir)
    report = provider.check_cache_integrity()
    assert "model_optimized.onnx" in report.missing_files


def test_check_cache_integrity_uses_fastembed_default_when_no_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``cache_dir`` is None, resolution falls through to the module default.

    Monkeypatches ``default_fastembed_cache_dir`` directly rather than the
    stdlib ``tempfile`` module, because ``tempfile.gettempdir`` caches its
    result early in the process and a TMPDIR env tweak does not reach it.
    """
    fake_cache = tmp_path / "fastembed_cache"  # does not exist
    monkeypatch.setattr(
        "engram.embedding.fastembed.default_fastembed_cache_dir",
        lambda: fake_cache,
    )
    provider = FastEmbedProvider()  # no explicit cache_dir
    report = provider.check_cache_integrity()
    # The path does not exist -> cache_dir resolves to None in the report.
    assert report.cache_dir is None
    assert report.snapshot_dir is None


# === sha256 helper ===


def test_sha256_of_file(tmp_path: Path):
    """SHA-256 stream-hashing for hash-pinning verification."""
    file_path = tmp_path / "blob.bin"
    file_path.write_bytes(b"hello")
    digest = _sha256_of_file(file_path)
    expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"  # pii-allow: hash
    assert digest == expected


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
