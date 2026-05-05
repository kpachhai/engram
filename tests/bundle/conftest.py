"""Shared bundle test fixtures - synthetic vaults populated for export/import."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from engram.models.frontmatter import Portability
from engram.storage.facade import VaultStorage
from engram.storage.sqlite import set_setting

DIM = 16


def _unit_vec(slot: int) -> list[float]:
    v = [0.0] * DIM
    v[slot % DIM] = 1.0
    return v


def make_vault_storage(*, base: Path, name: str) -> VaultStorage:
    """Open a fresh VaultStorage rooted at ``base / name``."""
    thoughts_dir = base / name / "thoughts"
    indexes_dir = base / name / ".indexes"
    thoughts_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    storage = VaultStorage(
        thoughts_dir=thoughts_dir,
        index_db_path=indexes_dir / "engram.db",
        embedding_dim=DIM,
        embedding_model_name="BAAI/bge-small-en-v1.5",
        vault_name=name,
    )
    set_setting(storage.conn, "embedding_model_name", "BAAI/bge-small-en-v1.5")
    set_setting(storage.conn, "embedding_dim", str(DIM))
    return storage


def populate_vault(storage: VaultStorage, *, thoughts: Iterable[tuple[str, Portability]]) -> None:
    for content, portability in thoughts:
        storage.capture(content=content, portability=portability, embedding=_unit_vec(0))


@pytest.fixture
def source_vault(tmp_path: Path) -> VaultStorage:
    storage = make_vault_storage(base=tmp_path, name="source")
    populate_vault(
        storage,
        thoughts=[
            ("[Pattern] portable one", "portable"),
            ("[Pattern] portable two", "portable"),
            ("[Domain] sensitive thing", "sensitive"),
            ("[Decision] block thing", "block"),
        ],
    )
    return storage


@pytest.fixture
def target_vault(tmp_path: Path) -> VaultStorage:
    return make_vault_storage(base=tmp_path, name="target")
