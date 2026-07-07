"""Tests for the signed_pull_required gate (trusted-keys verify-commit).

Regression: ``sync.signed_pull_required`` was a documented, configurable
control with a startup-probe WARN - but no pull path ever called
``gitops.verify_commit``, and ``verify_commit`` itself parsed VALIDSIG
with an index that can never match real gpg output. An operator who
enabled the control still pulled unsigned/attacker-signed commits.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.sync import gitops
from engram.sync.coordinator import CoordinatorConfig, SyncCoordinator
from engram.sync.gitops import GitErrorClass

from .conftest import commit_file, init_repo, run_git

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint
OTHER_FP = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"  # pii-allow: synthetic test fingerprint


# === verify_commit VALIDSIG parsing ===


def test_verify_commit_parses_real_gnupg_validsig_format() -> None:
    """`[GNUPG:] VALIDSIG <fpr> ...` must match; the old parse never could.

    Real `git verify-commit --raw` lines start with the `[GNUPG:]` token,
    so `parts[0].endswith("VALIDSIG")` was always False and the function
    returned False for every validly signed commit.
    """
    raw = (
        "[GNUPG:] NEWSIG\n"
        f"[GNUPG:] GOODSIG {VALID_FP[-16:]} Engram Test <t@example.com>\n"
        f"[GNUPG:] VALIDSIG {OTHER_FP} 2026-07-07 1751851200 0 4 0 22 8 00 {VALID_FP}\n"
        "[GNUPG:] TRUST_ULTIMATE 0 pgp\n"
    )

    class _CP:
        returncode = 0
        stdout = ""
        stderr = raw

    async def fake_git(*_args: object, **_kwargs: object) -> _CP:
        return _CP()

    original = gitops._git
    gitops._git = fake_git  # type: ignore[assignment]
    try:
        # Allow-list carries the PRIMARY fp (last VALIDSIG field).
        ok = asyncio.run(gitops.verify_commit(Path("/tmp"), "HEAD", [VALID_FP]))
        assert ok is True
        # A fingerprint on neither field refuses.
        bad = asyncio.run(
            gitops.verify_commit(
                Path("/tmp"),
                "HEAD",
                ["9999999988887777666655554444333322221111"],  # pii-allow: synthetic
            )
        )
        assert bad is False
    finally:
        gitops._git = original


# === trusted-keys loading ===


def test_load_trusted_keys_accepts_list_and_mapping(tmp_path: Path) -> None:
    plain = tmp_path / "plain.yaml"
    plain.write_text(f"- {VALID_FP}\n- {OTHER_FP}\n", encoding="utf-8")
    assert set(gitops.load_trusted_keys(plain)) == {VALID_FP, OTHER_FP}

    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(f"trusted_keys:\n  - {VALID_FP}\n", encoding="utf-8")
    assert gitops.load_trusted_keys(mapping) == [VALID_FP]

    assert gitops.load_trusted_keys(tmp_path / "absent.yaml") == []


# === signed_pull_gate ===


def _make_remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Bare remote with one (unsigned) commit + a local clone tracking it."""
    work = tmp_path / "work"
    init_repo(work, bare=False)
    commit_file(work, "seed.md", "seed")
    remote = tmp_path / "remote.git"
    init_repo(remote, bare=True)
    assert run_git(["remote", "add", "origin", str(remote)], work).returncode == 0
    assert run_git(["push", "origin", "main"], work).returncode == 0

    local = tmp_path / "local"
    cp = run_git(["clone", str(remote), str(local)], tmp_path)
    assert cp.returncode == 0, cp.stderr
    # New unsigned commit on the remote for the local to pull.
    commit_file(work, "new.md", "new content")
    assert run_git(["push", "origin", "main"], work).returncode == 0
    return remote, local


def test_signed_pull_gate_off_returns_none(tmp_path: Path) -> None:
    _remote, local = _make_remote_and_clone(tmp_path)
    reason = asyncio.run(
        gitops.signed_pull_gate(
            local,
            remote="origin",
            branch="main",
            signed_pull_required=False,
            trusted_keys_path=tmp_path / "absent.yaml",
        )
    )
    assert reason is None


def test_signed_pull_gate_refuses_empty_allowlist(tmp_path: Path) -> None:
    _remote, local = _make_remote_and_clone(tmp_path)
    reason = asyncio.run(
        gitops.signed_pull_gate(
            local,
            remote="origin",
            branch="main",
            signed_pull_required=True,
            trusted_keys_path=tmp_path / "absent.yaml",
        )
    )
    assert reason is not None
    assert "trusted-keys" in reason


def test_signed_pull_gate_refuses_unsigned_remote_head(tmp_path: Path) -> None:
    """The core control: unsigned remote head + gate on -> pull refused."""
    _remote, local = _make_remote_and_clone(tmp_path)
    keys = tmp_path / "trusted-keys.yaml"
    keys.write_text(f"- {VALID_FP}\n", encoding="utf-8")
    reason = asyncio.run(
        gitops.signed_pull_gate(
            local,
            remote="origin",
            branch="main",
            signed_pull_required=True,
            trusted_keys_path=keys,
        )
    )
    assert reason is not None


def test_explicit_pull_refuses_unsigned_when_gate_on(tmp_path: Path) -> None:
    """Coordinator wiring: explicit_pull must consult the gate."""
    _remote, local = _make_remote_and_clone(tmp_path)
    keys = tmp_path / "trusted-keys.yaml"
    keys.write_text(f"- {VALID_FP}\n", encoding="utf-8")
    coord = SyncCoordinator(
        repo_dir=local,
        config=CoordinatorConfig(
            signed_pull_required=True,
            trusted_keys_path=keys,
        ),
    )
    result = asyncio.run(coord.explicit_pull())
    assert result.error_class is GitErrorClass.SIGNATURE_UNVERIFIED
    # The unsigned commit must NOT have been rebased into the local tree.
    assert not (local / "new.md").exists()


# === real-GPG accept path ===

_HAS_GPG = shutil.which("gpg") is not None and shutil.which("gpgconf") is not None


@pytest.fixture
def gpg_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Ephemeral GNUPGHOME on a short path (gpg-agent socket limit)."""
    base = Path(tempfile.mkdtemp(prefix="engram-gpg-", dir="/tmp"))
    home = base / "g"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    env = {**os.environ, "GNUPGHOME": str(home)}
    yield env
    subprocess.run(
        ["gpgconf", "--kill", "gpg-agent"],  # noqa: S607
        env=env,
        capture_output=True,
        check=False,
    )
    shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(not _HAS_GPG, reason="gpg/gpgconf not installed")
def test_signed_pull_gate_accepts_trusted_signed_head(
    tmp_path: Path, gpg_env: dict[str, str]
) -> None:
    """A remote head really signed by an allow-listed key passes the gate."""
    cp = subprocess.run(
        [  # noqa: S607
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Engram Sync Test <engram-sync@example.com>",
            "ed25519",
            "sign",
            "never",
        ],
        env=gpg_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0, cp.stderr
    cp = subprocess.run(
        ["gpg", "--list-secret-keys", "--with-colons"],  # noqa: S607
        env=gpg_env,
        capture_output=True,
        text=True,
        check=False,
    )
    fps = [ln.split(":")[9] for ln in cp.stdout.splitlines() if ln.startswith("fpr:")]
    primary_fp = fps[0]

    _remote, local = _make_remote_and_clone(tmp_path)
    work = tmp_path / "work"
    assert run_git(["config", "user.signingkey", primary_fp], work).returncode == 0
    assert run_git(["config", "commit.gpgsign", "true"], work).returncode == 0
    (work / "signed.md").write_text("signed content")
    assert run_git(["add", "signed.md"], work).returncode == 0
    cp2 = subprocess.run(
        ["git", "commit", "-m", "signed"],  # noqa: S607
        cwd=work,
        env={
            **gpg_env,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp2.returncode == 0, cp2.stderr
    assert run_git(["push", "origin", "main"], work).returncode == 0

    keys = tmp_path / "trusted-keys.yaml"
    keys.write_text(f"- {primary_fp}\n", encoding="utf-8")
    reason = asyncio.run(
        gitops.signed_pull_gate(
            local,
            remote="origin",
            branch="main",
            signed_pull_required=True,
            trusted_keys_path=keys,
        )
    )
    assert reason is None, reason
