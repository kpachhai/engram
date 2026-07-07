"""Real-GPG integration tests for the team-vault pre-receive hook.

Hermetic despite using the real ``gpg`` binary: every test generates an
ephemeral keypair (Cert-only primary key + separate signing subkey - the
standard GPG setup) inside a per-test ``GNUPGHOME``; the user's keyring
is never touched. Skipped when ``gpg`` is not installed.

These tests exist because the pre-receive hook is security-boundary
code: the fingerprint-parsing and attribution/enrollment checks must be
exercised against real ``gpg`` status output and real signed commits,
not mocked VALIDSIG lines.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.team.server_hooks.pre_receive import run_hook

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgconf") is None,
    reason="gpg/gpgconf not installed",
)

_ZERO_SHA = "0" * 40
OTHER_FP = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"  # pii-allow: synthetic test fingerprint


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test-only helper, static commands
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def gpg_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Ephemeral GNUPGHOME; exported so the hook's subprocesses inherit it.

    Deliberately NOT under pytest's ``tmp_path``: gpg-agent's Unix socket
    path must stay under the ~104-byte sun_path limit, and tmp_path is
    routinely longer. A short mkdtemp dir (removed on teardown) keeps the
    test hermetic while keeping the socket path legal.
    """
    base = Path(
        tempfile.mkdtemp(
            prefix="engram-gpg-",
            dir="/tmp" if os.access("/tmp", os.W_OK) else None,
        )
    )
    home = base / "g"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    env = {**os.environ, "GNUPGHOME": str(home)}
    yield env
    _run(["gpgconf", "--kill", "gpg-agent"], env=env)
    shutil.rmtree(base, ignore_errors=True)


def _gen_key_with_signing_subkey(env: dict[str, str]) -> tuple[str, str]:
    """Generate a Cert-only primary + signing subkey; return (primary_fp, subkey_fp)."""
    cp = _run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Engram Test <engram-test@example.com>",
            "ed25519",
            "cert",
            "never",
        ],
        env=env,
    )
    assert cp.returncode == 0, cp.stderr
    cp = _run(["gpg", "--list-secret-keys", "--with-colons"], env=env)
    fprs = [ln.split(":")[9] for ln in cp.stdout.splitlines() if ln.startswith("fpr:")]
    assert fprs, cp.stdout
    primary_fp = fprs[0]

    cp = _run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-add-key",
            primary_fp,
            "ed25519",
            "sign",
            "never",
        ],
        env=env,
    )
    assert cp.returncode == 0, cp.stderr
    cp = _run(["gpg", "--list-secret-keys", "--with-colons"], env=env)
    fprs = [ln.split(":")[9] for ln in cp.stdout.splitlines() if ln.startswith("fpr:")]
    subkey_fp = fprs[-1]
    assert subkey_fp != primary_fp
    return primary_fp, subkey_fp


def _git(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    cp = _run(["git", *args], cwd=cwd, env=env)
    assert cp.returncode == 0, f"git {' '.join(args)}: {cp.stderr}"
    return cp.stdout


def _policy_yaml(steward_fp: str) -> str:
    return f"""\
allowed_prefixes: null
allowed_sources: null
accept_sensitive: false
required_embedding_model: BAAI/bge-small-en-v1.5
required_embedding_dim: 384
stewards:
  - {steward_fp}
min_engram_version: 0.4.0
"""


def _members_yaml(member_fp: str) -> str:
    return f"""\
members:
  - fingerprint: {member_fp}
    display_name: steward
revoked: []
"""


def _thought_md(captured_by: str) -> str:
    return f"""\
---
schema_version: 1
id: "0197a000-0000-7000-8000-000000000001"
prefix: Lesson
portability: portable
source: engram-user
created_at: "2026-07-07T00:00:00+00:00"
updated_at: "2026-07-07T00:00:00+00:00"
fingerprint: "{"a" * 64}"
captured_by: "{captured_by}"
---
[Lesson] real gpg integration body
"""


def _make_signed_team_repo(
    tmp_path: Path,
    env: dict[str, str],
    *,
    subkey_fp: str,
    steward_fp: str,
    member_fp: str,
    captured_by: str,
) -> tuple[Path, str]:
    """Init a repo with canonical team files + one thought, subkey-signed."""
    repo = tmp_path / "team-repo"
    repo.mkdir()
    _git(["init", "--initial-branch=main"], repo, env)
    _git(["config", "user.email", "engram-test@example.com"], repo, env)
    _git(["config", "user.name", "Engram Test"], repo, env)
    # Trailing '!' pins git to the signing subkey (standard separate-subkey setup).
    _git(["config", "user.signingkey", subkey_fp + "!"], repo, env)
    _git(["config", "commit.gpgsign", "true"], repo, env)

    engram_dir = repo / ".engram"
    engram_dir.mkdir()
    (engram_dir / "team-policy.yaml").write_text(_policy_yaml(steward_fp), encoding="utf-8")
    (engram_dir / "members.yaml").write_text(_members_yaml(member_fp), encoding="utf-8")
    thoughts = repo / "thoughts"
    thoughts.mkdir()
    (thoughts / "lesson.md").write_text(_thought_md(captured_by), encoding="utf-8")

    _git(["add", "."], repo, env)
    _git(["commit", "-m", "seed team vault"], repo, env)
    sha = _git(["rev-parse", "HEAD"], repo, env).strip()
    return repo, sha


def test_subkey_signed_push_accepted_with_primary_attribution(
    tmp_path: Path, gpg_env: dict[str, str]
) -> None:
    """A push signed with a separate signing subkey must be accepted when
    captured_by carries the PRIMARY fingerprint (pinned invariant 5)."""
    primary_fp, subkey_fp = _gen_key_with_signing_subkey(gpg_env)
    repo, sha = _make_signed_team_repo(
        tmp_path,
        gpg_env,
        subkey_fp=subkey_fp,
        steward_fp=primary_fp,
        member_fp=primary_fp,
        captured_by=primary_fp,
    )

    code, stderr = run_hook(
        stdin_text=f"{_ZERO_SHA} {sha} refs/heads/main\n",
        repo_path=str(repo),
    )

    assert code == 0, f"legitimate subkey-signed push refused:\n{stderr}"


def test_mismatched_captured_by_still_refused(tmp_path: Path, gpg_env: dict[str, str]) -> None:
    """Impersonation guard: captured_by naming a different primary fp refuses."""
    primary_fp, subkey_fp = _gen_key_with_signing_subkey(gpg_env)
    repo, sha = _make_signed_team_repo(
        tmp_path,
        gpg_env,
        subkey_fp=subkey_fp,
        steward_fp=primary_fp,
        member_fp=primary_fp,
        captured_by=OTHER_FP,
    )

    code, stderr = run_hook(
        stdin_text=f"{_ZERO_SHA} {sha} refs/heads/main\n",
        repo_path=str(repo),
    )

    assert code == 1
    assert "attribution_committer_mismatch" in stderr
