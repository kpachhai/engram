"""Hermetic CLI smoke for ``engram move-thought`` against the installed binary.

Regression: the registered command used to be a stub that parsed its
arguments, printed an explanatory message, and exited 0 WITHOUT moving
anything - automation treated the no-op as a completed relocation. The
smoke drives the real binary end-to-end: the thought must actually land
in the target vault and leave a tombstone behind.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.storage.facade import VaultStorage

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint


def _engram_bin() -> str:
    binary = shutil.which("engram")
    if binary is None:
        pytest.skip("engram binary not on PATH; run `uv sync` then `uv pip install -e .`")
    return binary


def _smoke_env(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "COLUMNS": "200",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


@pytest.fixture
def two_vault_home() -> Iterator[tuple[Path, Path, Path]]:
    """Short-path HOME with a primary vault + a team-write vault configured."""
    with tempfile.TemporaryDirectory(prefix="eng-smk-mv-", dir="/tmp") as root:
        home = Path(root)
        src = home / "vault-a"
        tgt = home / "vault-b"
        for vault in (src, tgt):
            (vault / "thoughts").mkdir(parents=True)
            (vault / ".indexes").mkdir(parents=True)
        cfg_dir = home / ".config" / "engram"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.yaml").write_text(
            f"""\
default_user: engram-test
vaults:
  - name: vault-a
    path: {src}
    role: primary
  - name: vault-b
    path: {tgt}
    role: team-write
    remote_url: git@example.com:team/vault-b.git
""",
            encoding="utf-8",
        )
        engram_dir = tgt / ".engram"
        engram_dir.mkdir()
        (engram_dir / "team-policy.yaml").write_text(
            f"""\
allowed_prefixes: null
allowed_sources: null
accept_sensitive: false
required_embedding_model: BAAI/bge-small-en-v1.5
required_embedding_dim: 384
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
        yield home, src, tgt


def test_move_thought_actually_moves(two_vault_home: tuple[Path, Path, Path]) -> None:
    """`engram move-thought A/<id> --to B` must relocate the thought, not no-op."""
    home, src, tgt = two_vault_home
    storage = VaultStorage(
        thoughts_dir=src / "thoughts",
        index_db_path=src / ".indexes" / "engram.db",
        vault_name="vault-a",
    )
    thought = storage.capture(content="[Lesson] move me across vaults")
    storage.close()

    result = subprocess.run(  # noqa: S603 - test-only, controlled args
        [
            _engram_bin(),
            "move-thought",
            f"vault-a/{thought.id}",
            "--to",
            "vault-b",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60.0,
        env=_smoke_env(home),
    )

    tgt_files = list((tgt / "thoughts").rglob("*.md"))
    moved = any(str(thought.id) in p.read_text(encoding="utf-8") for p in tgt_files)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    assert moved, (
        f"thought did not land in target vault (exit={result.returncode}); "
        f"a 0-exit without relocation is the stub regression. "
        f"stderr: {result.stderr!r}"
    )
    src_texts = [p.read_text(encoding="utf-8") for p in (src / "thoughts").rglob("*.md")]
    assert any("[MovedTo]" in text for text in src_texts), "no tombstone left in source"
    # The tombstone references the id, so check the original BODY is gone.
    assert not any("move me across vaults" in text for text in src_texts), "original not removed"


def test_move_thought_bad_ref_exits_nonzero(two_vault_home: tuple[Path, Path, Path]) -> None:
    """A ref without <vault>/<id> shape must exit non-zero."""
    home, _src, _tgt = two_vault_home
    result = subprocess.run(  # noqa: S603 - test-only, controlled args
        [_engram_bin(), "move-thought", "not-a-ref", "--to", "vault-b"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60.0,
        env=_smoke_env(home),
    )
    assert result.returncode != 0


def test_move_thought_registered_in_help() -> None:
    """move-thought must appear in `engram --help`.

    Guards the wiring itself: registration used to hide behind a
    hasattr() guard that would silently drop the command on a future
    rename instead of failing loudly at import like every peer.
    """
    result = subprocess.run(  # noqa: S603 - test-only, controlled args
        [_engram_bin(), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30.0,
        env={**os.environ, "COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"},
    )
    assert result.returncode == 0
    assert "move-thought" in result.stdout
