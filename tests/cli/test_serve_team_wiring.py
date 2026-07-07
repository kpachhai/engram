"""End-to-end serve-path wiring test: team-write capture over the built server.

Regression for the deferred-wiring gap where ``_build_multivault_server_for``
never populated ``HandlerDeps.team_policies`` / ``team_members`` /
``gpg_identity``, so every capture routed to a team-write vault through
``engram serve`` failed with a generic tool error while the handler-level
tests (which compose HandlerDeps in-process) stayed green.

This test drives the REAL builder + the REAL registered MCP tool via the
in-memory fastmcp Client, so the wiring itself is what is under test.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest
from fastmcp import Client

from engram.cli.serve import _build_multivault_server_for
from engram.config.models import (
    AggregatorConfig,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
    VaultMount,
)
from engram.storage.facade import VaultStorage
from engram.storage.sqlite import set_setting

DIM = 16
MODEL = "BAAI/bge-small-en-v1.5"
VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint


class _FakeEmbedder:
    dimension: int = DIM
    model_name: str = MODEL

    def embed(self, text: str) -> list[float]:
        del text
        v = [0.0] * DIM
        v[0] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)

    def warmup(self) -> None:
        pass

    def embed_batch(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class _FakeGpg:
    """Hermetic stand-in for GpgIdentity (no real keyring access)."""

    def primary_fingerprint(self) -> str:
        return VALID_FP


def _make_primary(tmp_path: Path) -> Path:
    p = tmp_path / "primary"
    (p / "thoughts").mkdir(parents=True)
    (p / ".indexes").mkdir(parents=True)
    return p


def _make_team_vault(tmp_path: Path, name: str) -> Path:
    """Scaffold a team-write vault with canonical .engram/ files on disk."""
    p = tmp_path / name
    (p / "thoughts").mkdir(parents=True)
    (p / ".indexes").mkdir(parents=True)
    engram_dir = p / ".engram"
    engram_dir.mkdir()
    (engram_dir / "team-policy.yaml").write_text(
        f"""\
allowed_prefixes: null
allowed_sources: null
accept_sensitive: false
required_embedding_model: {MODEL}
required_embedding_dim: {DIM}
stewards:
  - {VALID_FP}
min_engram_version: 0.4.0
""",
        encoding="utf-8",
    )
    (engram_dir / "members.yaml").write_text(
        f"""\
members:
  - fingerprint: {VALID_FP}
    display_name: steward
revoked: []
""",
        encoding="utf-8",
    )
    return p


def _config(tmp_path: Path, primary: Path, team: Path) -> EffectiveConfig:
    return EffectiveConfig(
        default_user="me",
        vault_path=primary,
        thoughts_dir=primary / "thoughts",
        index_dir=primary / ".indexes",
        embedding_model=MODEL,
        vault_name="primary",
        sync=SyncConfig(),
        llm=LLMConfig(provider="ollama"),
        aggregator=AggregatorConfig(min_per_vault_results=1),
        vaults=[
            VaultMount(name="primary", path=primary, role="primary"),
            VaultMount(
                name="team-x",
                path=team,
                role="team-write",
                remote_url="git@example:team-x.git",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_serve_builder_wires_team_deps_for_team_write_capture(
    tmp_path: Path,
) -> None:
    """A capture routed to a mounted team-write vault must succeed via serve."""
    primary_path = _make_primary(tmp_path)
    team_path = _make_team_vault(tmp_path, "team-x")
    config = _config(tmp_path, primary_path, team_path)

    primary_storage = VaultStorage(
        thoughts_dir=primary_path / "thoughts",
        index_db_path=primary_path / ".indexes" / "engram.db",
        embedding_dim=DIM,
        embedding_model_name=MODEL,
        vault_name="primary",
    )
    set_setting(primary_storage.conn, "embedding_model_name", MODEL)
    set_setting(primary_storage.conn, "embedding_dim", str(DIM))

    server = _build_multivault_server_for(
        config=config,
        primary_storage=primary_storage,
        embedder=_FakeEmbedder(),
        primary_coordinator=None,
        gpg_identity=_FakeGpg(),
    )
    try:
        async with Client(server) as client:
            result = await client.call_tool(
                "capture_thought",
                {
                    "content": "[Lesson] team capture through the serve path",
                    "metadata": {"vault": "team-x"},
                },
            )
        assert result.data["vault_name"] == "team-x"

        md_files = list((team_path / "thoughts").rglob("*.md"))
        assert len(md_files) == 1
        # captured_by must be stamped with the operator's primary fp.
        assert VALID_FP in md_files[0].read_text(encoding="utf-8")
    finally:
        primary_storage.close()
