"""Phase 3 multi-vault test harness fixtures.

Per Phase 3 plan Step 19 (verifier subsection of Layer G), this conftest
defines the integration scaffolding multi-vault tests reuse:

* :func:`make_vault_storage` - spin a one-off
  :class:`engram.storage.facade.VaultStorage` under a sub-path of the
  test's tmp_path; tests mount these into a registry.
* :func:`mount_vaults` - convenience for building a registry from a list
  of ``(name, role)`` pairs.
* :func:`build_query_vec` / :func:`build_thought_vec` - deterministic
  synthetic vectors so similarity-based tests don't need a live
  embedding model.

The fixtures here are intentionally hermetic: no network, no FastEmbed,
no real embedding model. ``embedding_model_name`` is set explicitly via
:func:`engram.storage.sqlite.set_setting` so the cross-vault embedding
compatibility check has rows to read.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import pytest

from engram.models.frontmatter import Portability
from engram.multivault.registry import VaultRegistry
from engram.storage.facade import VaultStorage
from engram.storage.sqlite import set_setting

DIM = 16


def _unit_vec(slot: int) -> list[float]:
    """One-hot unit vector under ``DIM`` so cosine-similarity is exact."""
    v = [0.0] * DIM
    v[slot % DIM] = 1.0
    return v


def make_vault_storage(
    *,
    base: Path,
    name: str,
    embedding_model: str = "BAAI/bge-small-en-v1.5",
    embedding_dim: int = DIM,
) -> VaultStorage:
    """Open a fresh VaultStorage rooted at ``base / name``."""
    thoughts_dir = base / name / "thoughts"
    indexes_dir = base / name / ".indexes"
    thoughts_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    storage = VaultStorage(
        thoughts_dir=thoughts_dir,
        index_db_path=indexes_dir / "engram.db",
        embedding_dim=embedding_dim,
        embedding_model_name=embedding_model,
        vault_name=name,
    )
    # Mirror the engram.cli.init step: stamp the embedding settings so
    # cross-vault compat checks have something to read.
    set_setting(storage.conn, "embedding_model_name", embedding_model)
    set_setting(storage.conn, "embedding_dim", str(embedding_dim))
    return storage


def populate_vault(
    storage: VaultStorage,
    *,
    thoughts: Iterable[tuple[str, Portability, int]],
) -> None:
    """Populate ``storage`` with simple synthetic thoughts.

    Each tuple is ``(content, portability, slot)`` where ``slot`` selects
    the one-hot vector dimension for the embedding. Slot 0 = matches the
    canonical query vector; slot 1+ = orthogonal (similarity 0.0).
    """
    for content, portability, slot in thoughts:
        storage.capture(
            content=content,
            portability=portability,
            embedding=_unit_vec(slot),
        )


@pytest.fixture
def two_vault_registry(tmp_path: Path) -> VaultRegistry:
    """Two vaults: ``primary`` (role primary) + ``alice`` (read-only)."""
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    populate_vault(
        primary,
        thoughts=[
            ("[Pattern] portable in primary slot 0", "portable", 0),
            ("[Domain] sensitive in primary", "sensitive", 0),
            ("[Decision] block in primary", "block", 0),
        ],
    )
    populate_vault(
        alice,
        thoughts=[
            ("[Pattern] portable in alice", "portable", 0),
            ("[Domain] sensitive in alice", "sensitive", 0),
        ],
    )
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    return registry


@pytest.fixture
def query_vec_top_slot() -> list[float]:
    """Query vector matching the slot-0 thoughts populated above."""
    return _unit_vec(0)


def fixed_query_vec(slot: int = 0) -> list[float]:
    """Module-level helper for tests that don't take the fixture."""
    return _unit_vec(slot)


def role_for(role: str) -> Literal["primary", "read-only"]:
    """Helper that narrows a string to the registry role literal."""
    if role == "primary":
        return "primary"
    if role == "read-only":
        return "read-only"
    msg = f"unknown role: {role!r}"
    raise ValueError(msg)
