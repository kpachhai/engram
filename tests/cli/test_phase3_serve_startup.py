"""Phase 3 multi-vault serve startup helpers (Step 17 verifier)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from engram.cli.serve_multivault import shutdown_multivault, startup_multivault
from engram.config.models import (
    AggregatorConfig,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
    VaultMount,
)


class _FakeEmbedder:
    dimension: int = 16
    model_name: str = "BAAI/bge-small-en-v1.5"

    def embed(self, text: str) -> list[float]:
        del text
        v = [0.0] * self.dimension
        v[0] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)

    def warmup(self) -> None:
        pass

    def embed_batch(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _make_vault_path(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    (p / "thoughts").mkdir(parents=True)
    (p / ".indexes").mkdir(parents=True)
    return p


def _config(tmp_path: Path, vaults: list[VaultMount]) -> EffectiveConfig:
    primary = next((v for v in vaults if v.role == "primary"), vaults[0])
    return EffectiveConfig(
        default_user="me",
        vault_path=primary.path,
        thoughts_dir=primary.path / "thoughts",
        index_dir=primary.path / ".indexes",
        embedding_model="BAAI/bge-small-en-v1.5",
        vault_name=primary.name,
        sync=SyncConfig(),
        llm=LLMConfig(),
        aggregator=AggregatorConfig(),
        vaults=vaults,
    )


def test_startup_mounts_all_vaults_skipping_probes(tmp_path: Path) -> None:
    """Without ``--skip-probes``, vaults that lack ``.git`` are mounted directly.

    The helper walks ``config.vaults`` in iteration order, opens each
    storage via the embedder, and registers them in the registry.
    """
    primary = _make_vault_path(tmp_path, "primary")
    alice = _make_vault_path(tmp_path, "alice")
    cfg = _config(
        tmp_path,
        [
            VaultMount(name="primary", path=primary, role="primary"),
            VaultMount(name="alice", path=alice, role="read-only"),
        ],
    )
    embedder = _FakeEmbedder()
    result = startup_multivault(cfg, embedder=embedder, skip_probes=True)
    try:
        assert {m.name for m in result.mounted} == {"primary", "alice"}
        assert result.registry.role_of("primary") == "primary"
        assert result.registry.role_of("alice") == "read-only"
        # Alice should have the read-only flag set on its storage.
        alice_storage = result.registry.get("alice")
        assert alice_storage is not None
        assert alice_storage.read_only_role is True
        primary_storage = result.registry.get("primary")
        assert primary_storage is not None
        assert primary_storage.read_only_role is False
    finally:
        shutdown_multivault(result)


def test_startup_skips_missing_vault_path(tmp_path: Path) -> None:
    """A vault path that doesn't exist is skipped; others continue."""
    primary = _make_vault_path(tmp_path, "primary")
    missing = tmp_path / "absent"  # never mkdir'd
    cfg = _config(
        tmp_path,
        [
            VaultMount(name="primary", path=primary, role="primary"),
            VaultMount(name="alice", path=missing, role="read-only"),
        ],
    )
    embedder = _FakeEmbedder()
    result = startup_multivault(cfg, embedder=embedder, skip_probes=True)
    try:
        assert "primary" in result.registry
        assert "alice" not in result.registry
        assert any(name == "alice" for name, _ in result.skipped)
    finally:
        shutdown_multivault(result)


def test_shutdown_closes_storages(tmp_path: Path) -> None:
    primary = _make_vault_path(tmp_path, "primary")
    cfg = _config(tmp_path, [VaultMount(name="primary", path=primary, role="primary")])
    result = startup_multivault(cfg, embedder=_FakeEmbedder(), skip_probes=True)
    shutdown_multivault(result)
    assert result.registry.names() == []
