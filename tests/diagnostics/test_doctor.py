"""Tests for engram.diagnostics.doctor."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.config.models import (
    DEFAULT_EMBEDDING_MODEL,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
)
from engram.diagnostics.doctor import CheckStatus, DoctorReport, run_diagnostics
from engram.embedding.model_hashes import KNOWN_MODEL_HASHES
from engram.embedding.protocol import EmbeddingProvider
from engram.storage.facade import VaultStorage

_DIM = 384


@pytest.fixture(autouse=True)
def _isolate_fastembed_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point FastEmbed's default cache resolution at a clean per-test directory.

    The ``embedding_cache_integrity`` doctor check resolves the cache via
    :func:`engram.embedding.fastembed.default_fastembed_cache_dir`.
    Monkeypatching the stdlib ``tempfile`` module is insufficient because
    its tempdir is cached early in the process; monkeypatching our
    explicit helper instead is surgical and reliable. Without this
    isolation the developer's actual FastEmbed cache leaks into test
    outcomes and the same suite produces different statuses on different
    machines.
    """
    fake_cache = tmp_path / "fastembed_cache"
    monkeypatch.setattr(
        "engram.embedding.fastembed.default_fastembed_cache_dir",
        lambda: fake_cache,
    )


class _StubEmbedder:
    """Deterministic stub conforming to EmbeddingProvider."""

    @property
    def model_name(self) -> str:
        return DEFAULT_EMBEDDING_MODEL

    @property
    def dimension(self) -> int:
        return _DIM

    def embed(self, text: str) -> list[float]:
        v = [0.0] * _DIM
        v[hash(text) % _DIM] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)


def _make_config(tmp_path: Path) -> EffectiveConfig:
    thoughts = tmp_path / "thoughts"
    indexes = tmp_path / ".indexes"
    thoughts.mkdir()
    indexes.mkdir()
    return EffectiveConfig(
        default_user="test-user",
        vault_path=tmp_path,
        thoughts_dir=thoughts,
        index_dir=indexes,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        vault_name="default",
        sync=SyncConfig(),
        llm=LLMConfig(),
    )


def _stub_factory(_config: EffectiveConfig) -> EmbeddingProvider:
    return _StubEmbedder()


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """Daemon-legal vault root.

    The daemon doctor rows treat an over-limit UDS socket path (104
    bytes on macOS) as a WARN, and pytest's tmp_path is routinely
    longer than that on macOS. Status-sensitive tests use this short
    mkdtemp root so "all green" stays achievable.
    """
    with tempfile.TemporaryDirectory(prefix="eng-doc-", dir="/tmp") as root:
        yield Path(root)


# === fresh vault: all green (or near-green) ===


def test_fresh_vault_all_ok(short_vault: Path):
    config = _make_config(short_vault)
    # Pre-create an empty SQLite db so the dim/model settings get recorded.
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["thoughts_dir"] is CheckStatus.OK
    assert statuses["index_dir"] is CheckStatus.OK
    assert statuses["sqlite_vec"] is CheckStatus.OK
    assert statuses["embedding_settings"] is CheckStatus.OK
    assert statuses["embedding_model"] is CheckStatus.OK
    assert statuses["index_consistency"] is CheckStatus.OK
    assert statuses["orphan_rows"] is CheckStatus.OK
    assert statuses["orphan_markdown"] is CheckStatus.OK
    assert statuses["orphan_tempfiles"] is CheckStatus.OK
    assert statuses["pending_embeddings"] is CheckStatus.OK
    assert report.exit_code == 0


def test_missing_thoughts_dir_is_fail(tmp_path: Path):
    config = _make_config(tmp_path)
    config.thoughts_dir.rmdir()
    report = run_diagnostics(config, embedder_factory=_stub_factory)
    fails = [c for c in report.checks if c.status is CheckStatus.FAIL]
    assert any(c.name == "thoughts_dir" for c in fails)
    assert report.exit_code == 2


def test_pending_embeddings_warn(short_vault: Path):
    config = _make_config(short_vault)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.capture(content="[Lesson] pending")  # no embedding -> pending
    storage.close()

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    pending_check = next(c for c in report.checks if c.name == "pending_embeddings")
    assert pending_check.status is CheckStatus.WARN
    assert report.exit_code == 1


def test_orphan_row_detected(tmp_path: Path):
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    captured = storage.capture(content="[Lesson] orphan", embedding=[0.0] * _DIM)
    captured.file_path.unlink()
    storage.close()

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    orphan_check = next(c for c in report.checks if c.name == "orphan_rows")
    assert orphan_check.status is CheckStatus.WARN


def test_orphan_markdown_detected(tmp_path: Path):
    """Markdown-on-disk with no SQLite row -> orphan_markdown WARN.

    Reproduces the 2026-05-13 -> 2026-05-16 incident class by hand:
    capture writes both, then we drop the SQLite row directly (mimicking
    the silent-swallow path where ``_q_insert_thought`` raised but
    markdown was already on disk).
    """
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    captured = storage.capture(content="[Lesson] mdorphan", embedding=[0.0] * _DIM)
    # Drop both SQLite rows for this thought; the markdown stays on disk.
    storage.conn.execute("DELETE FROM thoughts WHERE id = ?", (str(captured.id),))
    storage.conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (str(captured.id),))
    assert captured.file_path.exists()
    storage.close()

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    orphan_md = next(c for c in report.checks if c.name == "orphan_markdown")
    assert orphan_md.status is CheckStatus.WARN
    assert "engram reindex" in orphan_md.message
    assert orphan_md.detail is not None
    assert str(captured.id) in orphan_md.detail


def test_orphan_tempfiles_warn(tmp_path: Path):
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()
    # Drop a stray .tmp file that mimics a crashed atomic write.
    (config.thoughts_dir / "lesson").mkdir()
    (config.thoughts_dir / "lesson" / "stray.md.abc.tmp").write_text("partial")

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    tempfile_check = next(c for c in report.checks if c.name == "orphan_tempfiles")
    assert tempfile_check.status is CheckStatus.WARN


def test_index_consistency_warn_when_disk_has_extra(tmp_path: Path):
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()
    # Drop a markdown file that's never indexed.
    (config.thoughts_dir / "lesson").mkdir()
    (config.thoughts_dir / "lesson" / "stray.md").write_text("---\nfoo: bar\n---\nbody\n")

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    consistency = next(c for c in report.checks if c.name == "index_consistency")
    assert consistency.status is CheckStatus.WARN


# === --repair flag ===


def test_repair_promotes_pending_to_ok(tmp_path: Path):
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.capture(content="[Lesson] pending")
    storage.close()

    report = run_diagnostics(config, repair=True, embedder_factory=_stub_factory)
    repair_check = next(c for c in report.checks if c.name == "repair")
    assert repair_check.status is CheckStatus.OK
    assert "regenerated 1" in repair_check.message


def test_repair_with_remove_orphans(tmp_path: Path):
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    captured = storage.capture(content="[Lesson] orphan", embedding=[0.0] * _DIM)
    captured.file_path.unlink()
    storage.close()

    report = run_diagnostics(
        config,
        repair=True,
        remove_orphans=True,
        embedder_factory=_stub_factory,
    )
    remove_check = next(c for c in report.checks if c.name == "remove_orphans")
    assert remove_check.status is CheckStatus.OK
    assert "removed 1" in remove_check.message


# === DoctorReport behavior ===


def test_exit_code_zero_on_all_ok(short_vault: Path):
    config = _make_config(short_vault)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()
    report = run_diagnostics(config, embedder_factory=_stub_factory)
    assert report.exit_code == 0


def test_exit_code_two_when_any_fail(tmp_path: Path):
    config = _make_config(tmp_path)
    config.thoughts_dir.rmdir()  # induces a FAIL
    report = run_diagnostics(config, embedder_factory=_stub_factory)
    assert report.exit_code == 2


def test_a_report_holding_no_rows_exits_two_in_both_modes():
    """A run that examined nothing cannot report a clean vault.

    Every other exit code answers a question about the vault. Zero rows
    answers none, so it is a wiring failure and never a pass - in the
    default mode as much as under --strict.
    """
    report = DoctorReport()
    assert report.exit_code == 2
    assert report.strict_exit_code == 2


def test_default_exit_code_keeps_treating_a_skip_as_clean():
    """The published contract: exit 0 does not distinguish skip from pass."""
    report = DoctorReport()
    report.add("ran", CheckStatus.OK, "fine")
    report.add("never_ran", CheckStatus.SKIP, "skipped (precondition absent)")
    assert report.exit_code == 0
    assert report.skipped == 1


def test_strict_exit_code_is_three_when_any_row_did_not_run():
    report = DoctorReport()
    report.add("ran", CheckStatus.OK, "fine")
    report.add("never_ran", CheckStatus.SKIP, "skipped (precondition absent)")
    assert report.strict_exit_code == 3


def test_strict_exit_code_is_zero_when_every_row_ran():
    report = DoctorReport()
    report.add("ran", CheckStatus.OK, "fine")
    assert report.strict_exit_code == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [(CheckStatus.WARN, 1), (CheckStatus.FAIL, 2)],
)
def test_a_real_degradation_outranks_a_skip_under_strict(status: CheckStatus, expected: int):
    """3 means "only unanswered questions"; a WARN or FAIL is the better news to report."""
    report = DoctorReport()
    report.add("never_ran", CheckStatus.SKIP, "skipped (precondition absent)")
    report.add("degraded", status, "something is wrong")
    assert report.strict_exit_code == expected


# === embedding_cache_integrity check ===


def _seed_snapshot(cache_root: Path, *, files: list[str]) -> Path:
    """Materialize a fake HF-cache snapshot under ``cache_root/fastembed_cache``.

    Mirrors the on-disk layout FastEmbed itself uses for
    BAAI/bge-small-en-v1.5 so the doctor check resolves the same snapshot
    it would in production. Each entry in ``files`` becomes a present
    regular file; entries the test omits stay absent (the broken-partial
    state we're trying to surface).
    """
    snapshot = (
        cache_root
        / "fastembed_cache"
        / "models--qdrant--bge-small-en-v1.5-onnx-q"
        / "snapshots"
        / "test-snapshot-id"
    )
    snapshot.mkdir(parents=True)
    for filename in files:
        (snapshot / filename).write_bytes(b"stub")
    return snapshot


def test_cache_integrity_ok_when_no_cache_exists(tmp_path: Path):
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    check = next(c for c in report.checks if c.name == "embedding_cache_integrity")
    assert check.status is CheckStatus.OK
    assert "no FastEmbed cache yet" in check.message


def test_cache_integrity_ok_when_snapshot_intact(tmp_path: Path):
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()

    expected = list(KNOWN_MODEL_HASHES[DEFAULT_EMBEDDING_MODEL].keys())
    _seed_snapshot(tmp_path, files=expected)

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    check = next(c for c in report.checks if c.name == "embedding_cache_integrity")
    assert check.status is CheckStatus.OK
    assert "snapshot intact" in check.message


def test_cache_integrity_warn_when_snapshot_partial(short_vault: Path, tmp_path: Path):
    """Reproduces the broken-partial cache mode: symlinks exist, blobs don't."""
    config = _make_config(short_vault)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()

    expected = list(KNOWN_MODEL_HASHES[DEFAULT_EMBEDDING_MODEL].keys())
    # Drop the model_optimized.onnx file - the exact failure mode that triggers
    # ONNX NO_SUCHFILE on first embed call.
    partial = [f for f in expected if f != "model_optimized.onnx"]
    # The cache-isolation autouse fixture points FastEmbed at tmp_path.
    _seed_snapshot(tmp_path, files=partial)

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    check = next(c for c in report.checks if c.name == "embedding_cache_integrity")
    assert check.status is CheckStatus.WARN
    assert "incomplete" in check.message
    assert check.detail is not None
    assert "model_optimized.onnx" in check.detail
    assert report.exit_code == 1


def test_cache_integrity_warn_when_symlink_target_missing(tmp_path: Path):
    """A symlink whose target blob is gone counts as missing.

    HuggingFace's cache layout stores files as symlinks into ``blobs/``.
    When the snapshot exists but the underlying blob is removed (the
    failure shape we hit in the wild), ``Path.exists`` returns False on
    the dangling symlink and the check should report WARN.
    """
    config = _make_config(tmp_path)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()

    intact_files = [
        f for f in KNOWN_MODEL_HASHES[DEFAULT_EMBEDDING_MODEL] if f != "model_optimized.onnx"
    ]
    snapshot = _seed_snapshot(tmp_path, files=intact_files)
    # Add the model_optimized.onnx as a symlink whose target was deleted.
    blob = tmp_path / "missing_blob"
    blob.write_bytes(b"placeholder")
    symlink = snapshot / "model_optimized.onnx"
    symlink.symlink_to(blob)
    blob.unlink()
    assert symlink.is_symlink()
    assert not symlink.exists()  # dangling

    report = run_diagnostics(config, embedder_factory=_stub_factory)
    check = next(c for c in report.checks if c.name == "embedding_cache_integrity")
    assert check.status is CheckStatus.WARN
    assert check.detail is not None
    assert "model_optimized.onnx" in check.detail


# === daemon-mode rows fold into the doctor report ===


def test_run_diagnostics_includes_daemon_rows(short_vault: Path):
    """Every ALL_DAEMON_CHECK_CODES row must appear in the doctor report.

    Regression: the daemon check functions existed (and were unit-tested)
    but had zero callers in src/, so `engram doctor` never surfaced stale
    sockets, bad socket perms, or over-long UDS paths.
    """
    from engram.diagnostics.check_codes import ALL_DAEMON_CHECK_CODES

    config = _make_config(short_vault)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()

    report = run_diagnostics(config, embedder_factory=_stub_factory, skip_sync_checks=True)

    names = {c.name for c in report.checks}
    missing = set(ALL_DAEMON_CHECK_CODES) - names
    assert not missing, f"daemon doctor rows missing from report: {sorted(missing)}"
