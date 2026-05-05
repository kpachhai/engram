"""Cross-vault aggregator tests.

Covers:

* ATTACH path under threshold (<=10 vaults).
* SEQUENTIAL path at 11 vaults (forced by ``force_sequential=True`` for
  speed; the natural threshold is also exercised in test_phase3_exit_criteria).
* Block thoughts NEVER appear in cross-vault results regardless of
  ``include_sensitive`` flag (the portability invariant).
* Per-vault floor preserves small vaults' top-3.
* Per-vault timeout produces a ``degraded_vaults`` entry.
* Vault attribution preserved on every result row.
"""

from __future__ import annotations

import time
from pathlib import Path

from engram.models.mcp import Filter
from engram.multivault.aggregator import (
    ATTACH_VAULT_COUNT_CEILING,
    AggregatorMode,
    aggregate_search,
)
from engram.multivault.registry import VaultRegistry
from tests.multivault.conftest import (
    DIM,
    fixed_query_vec,
    make_vault_storage,
    populate_vault,
)


def _vec(slot: int) -> list[float]:
    v = [0.0] * DIM
    v[slot % DIM] = 1.0
    return v


def test_attach_path_under_threshold(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    populate_vault(
        primary,
        thoughts=[("[Pattern] one", "portable", 0), ("[Pattern] two", "portable", 0)],
    )
    populate_vault(alice, thoughts=[("[Pattern] alice-one", "portable", 0)])

    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")

    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=5,
    )
    assert result.mode_used == AggregatorMode.ATTACH
    assert len(result.rows) <= 5
    assert all(r.thought.portability != "block" for r in result.rows)
    primary.close()
    alice.close()


def test_sequential_path_when_forced(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    populate_vault(primary, thoughts=[("[Pattern] one", "portable", 0)])
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=5,
        force_sequential=True,
    )
    assert result.mode_used == AggregatorMode.SEQUENTIAL
    primary.close()


def test_sequential_path_at_eleven_vaults(tmp_path: Path) -> None:
    """At 11 vaults the aggregator drops to SEQUENTIAL mode."""
    from typing import Literal as _Lit

    storages = []
    registry = VaultRegistry()
    for i in range(11):
        s = make_vault_storage(base=tmp_path, name=f"v{i}")
        storages.append(s)
        populate_vault(s, thoughts=[(f"[Pattern] thought {i}", "portable", 0)])
        role: _Lit["primary", "read-only"] = "primary" if i == 0 else "read-only"
        registry.mount(name=f"v{i}", storage=s, role=role)
    assert len(registry) > ATTACH_VAULT_COUNT_CEILING
    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=5,
    )
    assert result.mode_used == AggregatorMode.SEQUENTIAL
    for s in storages:
        s.close()


def test_block_thought_never_in_cross_vault_default(tmp_path: Path) -> None:
    """Slot 0 has both portable + block; cross-vault search excludes block."""
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    populate_vault(
        primary,
        thoughts=[
            ("[Decision] block-tagged", "block", 0),
            ("[Pattern] portable-tagged", "portable", 0),
        ],
    )
    populate_vault(alice, thoughts=[("[Pattern] alice-portable", "portable", 0)])

    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")

    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=10,
    )
    assert all(r.thought.portability != "block" for r in result.rows)
    primary.close()
    alice.close()


def test_block_thought_never_in_cross_vault_with_include_sensitive(tmp_path: Path) -> None:
    """include_sensitive=True still does NOT permit block (invariant)."""
    primary = make_vault_storage(base=tmp_path, name="primary")
    populate_vault(
        primary,
        thoughts=[
            ("[Decision] block thought", "block", 0),
            ("[Domain] sensitive thought", "sensitive", 0),
            ("[Pattern] portable thought", "portable", 0),
        ],
    )
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=10,
        include_sensitive=True,
    )
    portabilities = {r.thought.portability for r in result.rows}
    assert "block" not in portabilities
    # sensitive should be present
    assert "sensitive" in portabilities
    primary.close()


def test_default_excludes_sensitive(tmp_path: Path) -> None:
    """Default cross-vault search returns portable only (invariant rule 1)."""
    primary = make_vault_storage(base=tmp_path, name="primary")
    populate_vault(
        primary,
        thoughts=[
            ("[Domain] sensitive", "sensitive", 0),
            ("[Pattern] portable", "portable", 0),
        ],
    )
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=10,
    )
    portabilities = {r.thought.portability for r in result.rows}
    assert portabilities == {"portable"}
    primary.close()


def test_per_vault_floor_three(tmp_path: Path) -> None:
    """Tiny vault still contributes its top-3 against a giant vault."""
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    # 50 thoughts in primary, slot 0 (all match query exactly)
    populate_vault(
        primary,
        thoughts=[(f"[Pattern] big-{i}", "portable", 0) for i in range(50)],
    )
    # 4 thoughts in alice
    populate_vault(
        alice,
        thoughts=[(f"[Pattern] alice-{i}", "portable", 0) for i in range(4)],
    )
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=10,
        min_per_vault_results=3,
    )
    alice_rows = [r for r in result.rows if r.vault_name == "alice"]
    assert len(alice_rows) >= 3, f"expected floor of 3 alice rows, got {len(alice_rows)}"
    primary.close()
    alice.close()


def test_vault_attribution_preserved(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    populate_vault(primary, thoughts=[("[Pattern] p", "portable", 0)])
    populate_vault(alice, thoughts=[("[Pattern] a", "portable", 0)])
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=10,
    )
    vault_names = {r.vault_name for r in result.rows}
    assert vault_names == {"primary", "alice"}
    # Each row's underlying thought.vault matches the row's vault_name.
    for r in result.rows:
        assert r.thought.vault == r.vault_name
    primary.close()
    alice.close()


def test_explicit_vault_filter_scopes_search(tmp_path: Path) -> None:
    primary = make_vault_storage(base=tmp_path, name="primary")
    alice = make_vault_storage(base=tmp_path, name="alice")
    populate_vault(primary, thoughts=[("[Pattern] p", "portable", 0)])
    populate_vault(alice, thoughts=[("[Pattern] a", "portable", 0)])
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    # Explicit single-vault filter
    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=10,
        filter_=Filter(vault="alice"),
    )
    assert {r.vault_name for r in result.rows} == {"alice"}
    primary.close()
    alice.close()


def test_per_vault_timeout_produces_degraded_marker(tmp_path: Path) -> None:
    """A vault that exceeds the timeout is added to ``degraded_vaults``."""
    primary = make_vault_storage(base=tmp_path, name="primary")
    populate_vault(primary, thoughts=[("[Pattern] one", "portable", 0)])

    # Wrap storage.search to introduce a delay; the aggregator's post-hoc
    # timeout check sees ``elapsed > aggregate_timeout_seconds`` and adds
    # the vault to ``degraded_vaults``.
    real_search = primary.search

    def slow_search(**kwargs):
        time.sleep(0.2)
        return real_search(**kwargs)

    primary.search = slow_search  # type: ignore[method-assign]

    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")

    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=10,
        aggregate_timeout_seconds=0.05,
    )
    assert "primary" in result.degraded_vaults
    primary.close()
