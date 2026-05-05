"""VaultRegistry tests.

* mount + get round-trip
* duplicate-name refusal
* primary singleton (zero / two raise; one OK)
* realpath collision after mount raises VaultPathCollision
* read-only vault write raises VaultReadOnlyError
* doctor repair against a read-only vault returns skipped count without crashing
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.errors import (
    DuplicateVaultName,
    VaultError,
    VaultPathCollision,
    VaultReadOnlyError,
)
from engram.multivault.registry import VaultRegistry
from tests.multivault.conftest import make_vault_storage, populate_vault


def test_mount_then_get(tmp_path: Path) -> None:
    storage = make_vault_storage(base=tmp_path, name="A")
    registry = VaultRegistry()
    registry.mount(name="A", storage=storage, role="primary")
    assert registry.get("A") is storage
    assert "A" in registry
    assert registry.role_of("A") == "primary"
    storage.close()


def test_mount_duplicate_name_refuses(tmp_path: Path) -> None:
    storage_a = make_vault_storage(base=tmp_path, name="A")
    storage_b = make_vault_storage(base=tmp_path, name="A_dup")
    registry = VaultRegistry()
    registry.mount(name="A", storage=storage_a, role="primary")
    with pytest.raises(DuplicateVaultName):
        registry.mount(name="A", storage=storage_b, role="read-only")
    storage_a.close()
    storage_b.close()


def test_primary_singleton_zero_raises(tmp_path: Path) -> None:
    storage = make_vault_storage(base=tmp_path, name="alice")
    registry = VaultRegistry()
    registry.mount(name="alice", storage=storage, role="read-only")
    with pytest.raises(VaultError):
        registry.primary()
    storage.close()


def test_primary_singleton_two_refused_at_mount(tmp_path: Path) -> None:
    storage_a = make_vault_storage(base=tmp_path, name="A")
    storage_b = make_vault_storage(base=tmp_path, name="B")
    registry = VaultRegistry()
    registry.mount(name="A", storage=storage_a, role="primary")
    with pytest.raises(VaultError):
        registry.mount(name="B", storage=storage_b, role="primary")
    storage_a.close()
    storage_b.close()


def test_primary_returns_storage(tmp_path: Path) -> None:
    storage = make_vault_storage(base=tmp_path, name="primary")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=storage, role="primary")
    assert registry.primary() is storage
    assert registry.primary_name() == "primary"
    storage.close()


def test_read_only_vaults_set(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    bob = make_vault_storage(base=tmp_path, name="bob")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    registry.mount(name="bob", storage=bob, role="read-only")
    assert registry.read_only_vaults() == {"alice", "bob"}
    primary.close()
    alice.close()
    bob.close()


def test_realpath_collision_after_mount_raises(tmp_path: Path) -> None:
    """Two vaults whose paths resolve to the same realpath are refused."""
    real = tmp_path / "real_vault"
    real.mkdir()
    link = tmp_path / "linked_vault"
    link.symlink_to(real)

    real_storage = make_vault_storage(base=real, name="primary")
    # Build a separate VaultStorage instance whose thoughts_dir is reachable
    # via the symlink even though the storages were constructed from
    # different textual paths.
    link_storage = make_vault_storage(base=link, name="primary")

    registry = VaultRegistry()
    registry.mount(name="A", storage=real_storage, role="primary")
    with pytest.raises(VaultPathCollision):
        registry.mount(name="B", storage=link_storage, role="read-only")
    real_storage.close()
    link_storage.close()


def test_read_only_vault_write_raises(tmp_path: Path) -> None:
    storage = make_vault_storage(base=tmp_path, name="alice")
    populate_vault(storage, thoughts=[("[Pattern] one", "portable", 0)])

    registry = VaultRegistry()
    registry.mount(name="alice", storage=storage, role="read-only")

    # Capture is the first write boundary.
    with pytest.raises(VaultReadOnlyError) as exc_info:
        storage.capture(content="[Decision] new")
    assert exc_info.value.error_code == "vault_read_only"

    # Update + delete also refused.
    rows, _ = storage.list_thoughts(limit=1)
    tid = rows[0].id
    with pytest.raises(VaultReadOnlyError):
        storage.update_metadata(tid, prefix="Domain")
    with pytest.raises(VaultReadOnlyError):
        storage.update_body(tid, new_content="something new")
    with pytest.raises(VaultReadOnlyError):
        storage.delete(tid)

    def _embed(_text: str) -> list[float]:  # pragma: no cover - never called
        return [0.0] * 16

    with pytest.raises(VaultReadOnlyError):
        storage.repair_pending_embeddings(_embed)


def test_unmount_idempotent(tmp_path: Path) -> None:
    storage = make_vault_storage(base=tmp_path, name="A")
    registry = VaultRegistry()
    registry.mount(name="A", storage=storage, role="primary")
    registry.unmount("A")
    registry.unmount("A")  # second call is a no-op
    assert registry.get("A") is None


def test_close_all_releases_storages(tmp_path: Path) -> None:
    storage_a = make_vault_storage(base=tmp_path, name="A")
    storage_b = make_vault_storage(base=tmp_path, name="B")
    registry = VaultRegistry()
    registry.mount(name="A", storage=storage_a, role="primary")
    registry.mount(name="B", storage=storage_b, role="read-only")
    registry.close_all()
    assert registry.names() == []
    assert registry.get("A") is None
    assert registry.get("B") is None


def test_storages_for_filter_default_routes_to_primary(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    selected = registry.storages_for_filter(None)
    assert len(selected) == 1
    assert selected[0][0] == "primary"
    primary.close()
    alice.close()


def test_storages_for_filter_star_returns_all(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    selected = registry.storages_for_filter("*")
    assert {n for n, _ in selected} == {"primary", "alice"}
    primary.close()
    alice.close()


def test_storages_for_filter_unknown_name_silently_dropped(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    selected = registry.storages_for_filter(["primary", "bogus"])
    assert {n for n, _ in selected} == {"primary"}
    primary.close()


def test_storages_for_filter_exact_match_only(tmp_path: Path) -> None:
    """vault filter is exact-match-only (no substring/prefix matching)."""
    personal = make_vault_storage(base=tmp_path, name="personal")
    archive = make_vault_storage(base=tmp_path, name="personal-archive")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=personal, role="primary")
    registry.mount(name="personal-archive", storage=archive, role="read-only")
    selected = registry.storages_for_filter("personal")
    assert {n for n, _ in selected} == {"personal"}
    personal.close()
    archive.close()


def test_doctor_repair_skipped_count_on_read_only_vault(tmp_path: Path) -> None:
    """Per plan: doctor surfaces 'skipped N pending embeddings' as INFO.

    Implementation contract: ``repair_pending_embeddings`` raises
    ``VaultReadOnlyError`` on a read-only-mounted vault. Doctor catches
    the exception and increments a per-vault skipped counter. This test
    asserts the storage-side behavior; the doctor-side counting is
    exercised in the doctor tests.
    """
    storage = make_vault_storage(base=tmp_path, name="alice")
    registry = VaultRegistry()
    registry.mount(name="alice", storage=storage, role="read-only")
    with pytest.raises(VaultReadOnlyError):
        storage.repair_pending_embeddings(lambda _: [0.0] * 16)
    storage.close()
