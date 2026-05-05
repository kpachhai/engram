"""Phase 3 cross-vault embedding compatibility tests (Step 7 verifier).

Per ``docs/PHASE_3_PLAN.md`` Step 7:

* Two vaults with the same model name + dim: OK.
* Two vaults with different models or different dims: refuses with
  EmbeddingModelMismatch.
* aggregate_search re-checks compat on every call (registry state may
  have changed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.errors import EmbeddingModelMismatch
from engram.multivault.aggregator import (
    aggregate_search,
    assert_compatible_embeddings,
)
from engram.multivault.registry import VaultRegistry
from engram.storage.sqlite import set_setting
from tests.multivault.conftest import (
    fixed_query_vec,
    make_vault_storage,
    populate_vault,
)


def test_same_model_passes(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    # Should not raise.
    assert_compatible_embeddings(registry)
    primary.close()
    alice.close()


def test_different_models_refuses(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice", embedding_model="some-other/model")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    with pytest.raises(EmbeddingModelMismatch) as exc_info:
        assert_compatible_embeddings(registry)
    assert exc_info.value.error_code == "embedding_model_mismatch"
    primary.close()
    alice.close()


def test_different_dims_refuses(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    # Manually overwrite alice's recorded dim to mimic schema drift.
    set_setting(alice.conn, "embedding_dim", "999")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    with pytest.raises(EmbeddingModelMismatch):
        assert_compatible_embeddings(registry)
    primary.close()
    alice.close()


def test_aggregate_search_refuses_on_mismatch(tmp_path: Path) -> None:
    """aggregate_search must call the compat check on every invocation."""
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice", embedding_model="some-other/model")
    populate_vault(primary, thoughts=[("[Pattern] p", "portable", 0)])
    populate_vault(alice, thoughts=[("[Pattern] a", "portable", 0)])
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    with pytest.raises(EmbeddingModelMismatch):
        aggregate_search(
            registry=registry,
            query_embedding=fixed_query_vec(0),
            k=10,
        )
    primary.close()
    alice.close()


def test_unset_settings_does_not_raise(tmp_path: Path) -> None:
    """Empty settings mean fresh vault; compat check skips rather than fails."""
    primary = make_vault_storage(base=tmp_path, name="primary")
    # Wipe alice's settings to simulate a fresh vault before any capture.
    alice = make_vault_storage(base=tmp_path, name="alice")
    alice.conn.execute("DELETE FROM engram_settings")
    alice.conn.commit()
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    # Should not raise: alice has no recorded model, so the check skips it.
    assert_compatible_embeddings(registry)
    primary.close()
    alice.close()
