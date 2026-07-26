"""Delete must not report success while the markdown source of truth survives.

Markdown is the source of truth and SQLite is a regenerable cache. Dropping the
index row while the file remains on disk inverts that: the caller is told the
thought is gone, and the next reindex re-imports it from the markdown that was
never removed.

The unremovable-file condition is produced with real filesystem permissions
(a non-writable parent directory) rather than by patching ``Path.unlink``,
which is used by unrelated machinery in the same process.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest

from engram.storage.facade import VaultStorage
from engram.storage.sqlite_queries import get_thought_row

_DIM = 384

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses directory write permissions",
)


def _zero_vec() -> list[float]:
    return [0.0] * _DIM


@pytest.fixture
def vault(tmp_path: Path) -> Generator[VaultStorage, None, None]:
    thoughts_dir = tmp_path / "thoughts"
    indexes_dir = tmp_path / ".indexes"
    thoughts_dir.mkdir()
    indexes_dir.mkdir()
    storage = VaultStorage(
        thoughts_dir=thoughts_dir,
        index_db_path=indexes_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name="BAAI/bge-small-en-v1.5",
        vault_name="default",
    )
    yield storage
    storage.close()


def test_failed_markdown_removal_is_a_failed_delete(vault: VaultStorage) -> None:
    """A delete whose SoT removal fails must raise and leave the index row.

    Reporting success here is what lets a "deleted" thought reappear on the
    next reindex.
    """
    thought = vault.capture(content="[Lesson] body", embedding=_zero_vec())
    holder = thought.file_path.parent
    original_mode = holder.stat().st_mode

    holder.chmod(0o500)  # readable + traversable, not writable: unlink fails
    try:
        with pytest.raises(OSError, match="Permission denied"):
            vault.delete(thought.id)

        assert thought.file_path.exists(), "precondition: markdown was not removed"
        assert get_thought_row(vault.conn, thought.id) is not None, (
            "index row was dropped even though the markdown source of truth remains"
        )
        assert vault.get_by_id(thought.id) is not None
    finally:
        holder.chmod(original_mode)
