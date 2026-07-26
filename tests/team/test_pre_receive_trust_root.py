"""Where the hook reads its trust root from, and what counts as a mutation.

Policy and membership must be read from the canonical state already in the
repository. Reading them out of the tree being pushed lets the pusher supply the
rules they are about to be judged against.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from engram.team.server_hooks.pre_receive import run_hook

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint
OTHER_FP = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"  # pii-allow: synthetic test fingerprint
_ZERO_SHA = "0" * 40

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=str(cwd),
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )


def _policy(steward: str) -> str:
    return (
        "allowed_prefixes:\n  - Postmortem\nallowed_sources: null\n"
        f"accept_sensitive: false\nstewards:\n  - {steward}\n"
    )


def _members(fingerprint: str) -> str:
    return f"members:\n  - fingerprint: {fingerprint}\n    display_name: someone\nrevoked: []\n"


def _thought(captured_by: str) -> str:
    return (
        "---\n"
        "id: 11111111-1111-1111-1111-111111111111\n"
        "prefix: Postmortem\n"
        "portability: portable\n"
        "source: engram-test\n"
        "created_at: 2026-01-01T00:00:00Z\n"
        "updated_at: 2026-01-01T00:00:00Z\n"
        f"fingerprint: {'a' * 64}\n"
        f"captured_by: {captured_by}\n"
        "---\n"
        "[Postmortem] body\n"
    )


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit_all(repo: Path, message: str) -> str:
    assert _git(["add", "-A"], repo).returncode == 0
    assert _git(["commit", "-m", message, "--no-verify"], repo).returncode == 0
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


@pytest.fixture
def team_repo(tmp_path: Path) -> Path:
    """A repo whose committed state enrolls VALID_FP and makes it steward."""
    repo = tmp_path / "team"
    repo.mkdir()
    assert _git(["init", "--initial-branch=main", "."], repo).returncode == 0
    _write(repo, ".engram/team-policy.yaml", _policy(VALID_FP))
    _write(repo, ".engram/members.yaml", _members(VALID_FP))
    _commit_all(repo, "team vault setup")
    return repo


def test_new_ref_cannot_supply_its_own_policy_and_membership(
    tmp_path: Path, team_repo: Path
) -> None:
    """Creating a branch must not reset the trust root to the pushed tree.

    ``old_sha`` is all zeros for ANY newly created ref, not just a repository's
    first push, so reading policy from the new tree lets a non-member enroll
    themselves and grant themselves steward on a throwaway branch.

    Modelled on a real remote: the bare repo's HEAD stays on the default branch
    while the attacker's objects arrive on a side branch.
    """
    bare = tmp_path / "remote.git"
    assert _git(["init", "--bare", "--initial-branch=main", str(bare)], tmp_path).returncode == 0
    assert _git(["push", str(bare), "main"], team_repo).returncode == 0

    assert _git(["checkout", "-b", "attacker"], team_repo).returncode == 0
    _write(team_repo, ".engram/team-policy.yaml", _policy(OTHER_FP))
    _write(team_repo, ".engram/members.yaml", _members(OTHER_FP))
    _write(team_repo, "thoughts/postmortem/x.md", _thought(OTHER_FP))
    attacker_sha = _commit_all(team_repo, "self-grant")
    # Land the objects in the remote without moving its HEAD.
    assert _git(["push", str(bare), "attacker"], team_repo).returncode == 0

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        return_value=OTHER_FP,
    ):
        code, stderr = run_hook(
            stdin_text=f"{_ZERO_SHA} {attacker_sha} refs/heads/attacker\n",
            repo_path=str(bare),
        )

    assert code != 0, (
        "push accepted: the pushed tree was allowed to define its own "
        f"stewards/membership. stderr={stderr!r}"
    )


def test_deleting_canonical_files_requires_steward(team_repo: Path) -> None:
    """Removing members.yaml must be as gated as modifying it.

    A diff filtered to added/modified/renamed/type-changed paths never sees a
    deletion, so the steward check could not fire - and the next push then finds
    the canonical files missing and refuses everything.
    """
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    (team_repo / ".engram" / "members.yaml").unlink()
    deleted_sha = _commit_all(team_repo, "drop members")

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        return_value=OTHER_FP,
    ):
        code, stderr = run_hook(
            stdin_text=f"{base_sha} {deleted_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code != 0, "a non-steward deleted members.yaml unchallenged"
    assert "steward" in stderr.lower(), stderr


def test_steward_may_still_update_canonical_files(team_repo: Path) -> None:
    """The deletion gate must not block legitimate steward maintenance."""
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    _write(team_repo, ".engram/members.yaml", _members(VALID_FP).replace("someone", "renamed"))
    updated_sha = _commit_all(team_repo, "rename member")

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        return_value=VALID_FP,
    ):
        code, stderr = run_hook(
            stdin_text=f"{base_sha} {updated_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code == 0, stderr


def test_block_portability_refused_regardless_of_case(team_repo: Path) -> None:
    """``portability: BLOCK`` must not slip past an exact-match comparison."""
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    _write(
        team_repo,
        "thoughts/postmortem/x.md",
        _thought(VALID_FP).replace("portability: portable", "portability: BLOCK"),
    )
    pushed_sha = _commit_all(team_repo, "sneak a block thought")

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        return_value=VALID_FP,
    ):
        code, stderr = run_hook(
            stdin_text=f"{base_sha} {pushed_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code != 0, "an uppercase block thought was accepted into the team vault"
    assert "block" in stderr.lower(), stderr
