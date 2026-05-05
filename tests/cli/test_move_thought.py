"""Tests for engram.cli.move_thought.move_thought_cmd.

Step 19 verifier: covers (a) happy path with id/created_at/captured_by
preservation, (b) source-chain prepend, (c) tombstone schema in source
vault, (d) policy refusal at target, (e) read-only refusal at either
side, (f) chain-depth-10 ceiling refusal, (g) same-vault refusal.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from engram.cli.move_thought import (
    MAX_MOVE_CHAIN_DEPTH,
    MoveThoughtError,
    move_thought_cmd,
)
from engram.errors import (
    BlockThoughtInTeamVaultDisallowed,
    TeamPolicyViolation,
    VaultReadOnlyError,
)
from engram.multivault.registry import VaultRegistry
from engram.storage.facade import VaultStorage
from engram.storage.sqlite import set_setting
from engram.team.policy import TeamVaultPolicy

DIM = 16
VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"


class _FakeEmbedder:
    dimension: int = DIM

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


def _vault(tmp_path: Path, name: str) -> VaultStorage:
    thoughts_dir = tmp_path / name / "thoughts"
    indexes_dir = tmp_path / name / ".indexes"
    thoughts_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    storage = VaultStorage(
        thoughts_dir=thoughts_dir,
        index_db_path=indexes_dir / "engram.db",
        embedding_dim=DIM,
        embedding_model_name="test-model",
        vault_name=name,
    )
    set_setting(storage.conn, "embedding_model_name", "test-model")
    set_setting(storage.conn, "embedding_dim", str(DIM))
    return storage


def _capture(
    storage: VaultStorage,
    content: str,
    *,
    portability: str = "portable",
    captured_by: str | None = None,
    source: str = "engram-test",
) -> str:
    v = [0.0] * DIM
    v[0] = 1.0
    t = storage.capture(
        content=content,
        portability=portability,  # type: ignore[arg-type]
        source=source,
        embedding=v,
        captured_by=captured_by,
    )
    return str(t.id)


def test_move_thought_preserves_id_and_metadata(tmp_path: Path) -> None:
    src = _vault(tmp_path, "personal")
    dst = _vault(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    registry.mount(name="team-x", storage=dst, role="team-write")
    tid = _capture(src, "[Postmortem] body", captured_by=VALID_FP)

    result = move_thought_cmd(
        registry=registry,
        source_vault="personal",
        target_vault="team-x",
        thought_id=tid,
    )
    # ID preserved.
    assert result.thought_id == tid
    moved = dst.get_by_id(tid)
    assert moved is not None
    # captured_by preserved.
    assert moved.captured_by == VALID_FP
    # source chain prepended.
    assert "moved-from:personal:" in moved.source
    assert tid in moved.source
    # Tombstone in source vault.
    assert result.tombstone_id != tid
    tombstone = src.get_by_id(result.tombstone_id)
    assert tombstone is not None
    assert tombstone.prefix == "MovedTo"
    assert "team-x" in tombstone.content


def test_move_thought_refuses_same_vault(tmp_path: Path) -> None:
    src = _vault(tmp_path, "personal")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    tid = _capture(src, "[Lesson] body")
    with pytest.raises(MoveThoughtError, match="same"):
        move_thought_cmd(
            registry=registry,
            source_vault="personal",
            target_vault="personal",
            thought_id=tid,
        )


def test_move_thought_refuses_unmounted_target(tmp_path: Path) -> None:
    src = _vault(tmp_path, "personal")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    tid = _capture(src, "[Lesson] body")
    with pytest.raises(MoveThoughtError, match="not mounted"):
        move_thought_cmd(
            registry=registry,
            source_vault="personal",
            target_vault="team-missing",
            thought_id=tid,
        )


def test_move_thought_refuses_read_only_target(tmp_path: Path) -> None:
    src = _vault(tmp_path, "personal")
    dst = _vault(tmp_path, "friend")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    registry.mount(name="friend", storage=dst, role="read-only")
    tid = _capture(src, "[Lesson] body")
    with pytest.raises(VaultReadOnlyError):
        move_thought_cmd(
            registry=registry,
            source_vault="personal",
            target_vault="friend",
            thought_id=tid,
        )


def test_move_thought_refuses_read_only_source(tmp_path: Path) -> None:
    src = _vault(tmp_path, "friend")
    dst = _vault(tmp_path, "personal")
    registry = VaultRegistry()
    registry.mount(name="friend", storage=src, role="read-only")
    registry.mount(name="personal", storage=dst, role="primary")
    # Disable read-only role temporarily to seed the source.
    src.set_read_only_role(read_only=False)
    tid = _capture(src, "[Lesson] body")
    src.set_read_only_role(read_only=True)
    with pytest.raises(VaultReadOnlyError, match="source"):
        move_thought_cmd(
            registry=registry,
            source_vault="friend",
            target_vault="personal",
            thought_id=tid,
        )


def test_move_thought_refuses_missing_id(tmp_path: Path) -> None:
    src = _vault(tmp_path, "personal")
    dst = _vault(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    registry.mount(name="team-x", storage=dst, role="team-write")
    # Use a valid UUID format but with no thought stored at it.
    fake_id = "00000000-0000-7000-8000-000000000000"
    with pytest.raises(MoveThoughtError, match="not found"):
        move_thought_cmd(
            registry=registry,
            source_vault="personal",
            target_vault="team-x",
            thought_id=fake_id,
        )


def test_move_thought_target_policy_refuses(tmp_path: Path) -> None:
    src = _vault(tmp_path, "personal")
    dst = _vault(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    registry.mount(name="team-x", storage=dst, role="team-write")
    tid = _capture(src, "[Friction] body")
    policy = TeamVaultPolicy(
        allowed_prefixes=["Postmortem"],
        required_embedding_model="test-model",
        required_embedding_dim=DIM,
    )
    with pytest.raises(TeamPolicyViolation):
        move_thought_cmd(
            registry=registry,
            source_vault="personal",
            target_vault="team-x",
            thought_id=tid,
            target_policy=policy,
        )


def test_move_thought_block_portability_refused_at_target(tmp_path: Path) -> None:
    src = _vault(tmp_path, "personal")
    dst = _vault(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    registry.mount(name="team-x", storage=dst, role="team-write")
    tid = _capture(src, "[Lesson] secret", portability="block")
    policy = TeamVaultPolicy(
        required_embedding_model="test-model",
        required_embedding_dim=DIM,
    )
    with pytest.raises(BlockThoughtInTeamVaultDisallowed):
        move_thought_cmd(
            registry=registry,
            source_vault="personal",
            target_vault="team-x",
            thought_id=tid,
            target_policy=policy,
        )


def test_move_thought_chain_depth_ceiling(tmp_path: Path) -> None:
    """A thought already moved 10 times refuses the 11th move."""
    src = _vault(tmp_path, "personal")
    chained_source = (
        "moved-from:a:1 <- moved-from:b:2 <- moved-from:c:3 <- "
        "moved-from:d:4 <- moved-from:e:5 <- moved-from:f:6 <- "
        "moved-from:g:7 <- moved-from:h:8 <- moved-from:i:9 <- "
        "moved-from:j:10 <- engram-test"
    )
    dst = _vault(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    registry.mount(name="team-x", storage=dst, role="team-write")
    tid = _capture(src, "[Lesson] body", source=chained_source)
    with pytest.raises(MoveThoughtError, match="ceiling"):
        move_thought_cmd(
            registry=registry,
            source_vault="personal",
            target_vault="team-x",
            thought_id=tid,
        )


def test_move_thought_increments_chain_depth(tmp_path: Path) -> None:
    """Each move adds one ``moved-from:`` prefix; depth tracks correctly."""
    src = _vault(tmp_path, "personal")
    dst = _vault(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="personal", storage=src, role="primary")
    registry.mount(name="team-x", storage=dst, role="team-write")
    tid = _capture(src, "[Lesson] body")
    result = move_thought_cmd(
        registry=registry,
        source_vault="personal",
        target_vault="team-x",
        thought_id=tid,
    )
    assert result.chain_depth == 1
    assert MAX_MOVE_CHAIN_DEPTH == 10
