"""Tests for engram.diagnostics.doctor."""

from __future__ import annotations

from pathlib import Path

from engram.config.models import (
    DEFAULT_EMBEDDING_MODEL,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
)
from engram.diagnostics.doctor import CheckStatus, run_diagnostics
from engram.embedding.protocol import EmbeddingProvider
from engram.storage.facade import VaultStorage

_DIM = 384


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


# === fresh vault: all green (or near-green) ===


def test_fresh_vault_all_ok(tmp_path: Path):
    config = _make_config(tmp_path)
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


def test_pending_embeddings_warn(tmp_path: Path):
    config = _make_config(tmp_path)
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


def test_exit_code_zero_on_all_ok(tmp_path: Path):
    config = _make_config(tmp_path)
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
