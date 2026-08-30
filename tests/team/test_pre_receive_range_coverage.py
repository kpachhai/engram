"""Everything in a pushed range is subject to the gates, not just its endpoints.

Comparing ``old_sha..new_sha`` as two trees hides whatever happened in between.
Content added and then removed inside the same push disappears from that diff
while its blobs stay reachable in the shared remote, and only the tip commit's
signature is ever checked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from engram.team.server_hooks import pre_receive
from engram.team.server_hooks.pre_receive import run_hook

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint
OTHER_FP = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"  # pii-allow: synthetic test fingerprint

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


def _policy() -> str:
    return (
        "allowed_prefixes:\n  - Postmortem\nallowed_sources: null\n"
        f"accept_sensitive: false\nstewards:\n  - {VALID_FP}\n"
    )


def _members() -> str:
    return f"members:\n  - fingerprint: {VALID_FP}\n    display_name: alice\nrevoked: []\n"


def _thought(portability: str = "portable", captured_by: str = VALID_FP) -> str:
    return (
        "---\n"
        "id: 11111111-1111-1111-1111-111111111111\n"
        "prefix: Postmortem\n"
        f"portability: {portability}\n"
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
    repo = tmp_path / "team"
    repo.mkdir()
    assert _git(["init", "--initial-branch=main", "."], repo).returncode == 0
    _write(repo, ".engram/team-policy.yaml", _policy())
    _write(repo, ".engram/members.yaml", _members())
    _commit_all(repo, "team vault setup")
    return repo


def test_block_thought_added_then_deleted_in_same_push_is_refused(team_repo: Path) -> None:
    """Transient content still lands in the shared remote's history."""
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    _write(team_repo, "thoughts/postmortem/secret.md", _thought(portability="block"))
    _commit_all(team_repo, "add block thought")
    (team_repo / "thoughts" / "postmortem" / "secret.md").unlink()
    tip_sha = _commit_all(team_repo, "remove it again")

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        return_value=VALID_FP,
    ):
        code, stderr = run_hook(
            stdin_text=f"{base_sha} {tip_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code != 0, (
        "block content was smuggled into the remote by deleting it in a later "
        "commit of the same push"
    )
    assert "block" in stderr.lower(), stderr


def test_intermediate_commit_by_non_member_is_refused(team_repo: Path) -> None:
    """Attributing a whole range to the tip signer skips the other commits."""
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    _write(team_repo, "thoughts/postmortem/a.md", _thought())
    middle_sha = _commit_all(team_repo, "middle commit")
    _write(team_repo, "thoughts/postmortem/b.md", _thought())
    tip_sha = _commit_all(team_repo, "tip commit")

    def _by_sha(sha: str, **_kwargs: object) -> str | None:
        # The tip is signed by an enrolled member; the middle commit is not.
        return VALID_FP if sha == tip_sha else OTHER_FP

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        side_effect=_by_sha,
    ):
        code, _stderr = run_hook(
            stdin_text=f"{base_sha} {tip_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code != 0, f"unsigned/non-member intermediate commit {middle_sha} accepted"


def test_push_without_thoughts_still_requires_an_enrolled_signer(team_repo: Path) -> None:
    """Identity was only enforced per thought file, so a push touching none was free."""
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    _write(team_repo, "notes.txt", "not a thought\n")
    tip_sha = _commit_all(team_repo, "unrelated file")

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        return_value=None,
    ):
        code, _stderr = run_hook(
            stdin_text=f"{base_sha} {tip_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code != 0, "an unsigned push touching no thoughts was accepted"


def test_enrolled_member_push_touching_no_thoughts_is_allowed(team_repo: Path) -> None:
    """Positive control: ordinary housekeeping by a member must still pass."""
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    _write(team_repo, "notes.txt", "not a thought\n")
    tip_sha = _commit_all(team_repo, "unrelated file")

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        return_value=VALID_FP,
    ):
        code, stderr = run_hook(
            stdin_text=f"{base_sha} {tip_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code == 0, stderr


def test_clean_multi_commit_push_by_member_is_allowed(team_repo: Path) -> None:
    """Positive control: per-commit checking must not reject a legitimate range."""
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    _write(team_repo, "thoughts/postmortem/a.md", _thought())
    _commit_all(team_repo, "first")
    _write(team_repo, "thoughts/postmortem/b.md", _thought())
    tip_sha = _commit_all(team_repo, "second")

    with patch(
        "engram.team.server_hooks.pre_receive._committer_fingerprint",
        return_value=VALID_FP,
    ):
        code, stderr = run_hook(
            stdin_text=f"{base_sha} {tip_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code == 0, stderr


def test_unreadable_thought_content_is_refused_not_skipped(team_repo: Path) -> None:
    """A git failure reading a thought must refuse the push, not skip the check.

    ``_ls_tree_at`` wrapped ``git show`` in a bare ``except RuntimeError`` and
    returned ``None`` for every failure alike, and both callers read ``None`` as
    "nothing here to validate". A timeout or an unreadable object therefore let
    the file past the gate unchecked - the wrong direction for a gate whose job
    is refusing bad pushes.
    """
    base_sha = _git(["rev-parse", "HEAD"], team_repo).stdout.strip()
    _write(team_repo, "thoughts/postmortem/note.md", _thought())
    tip_sha = _commit_all(team_repo, "add a thought")

    real_git_cmd = pre_receive._git_cmd

    def flaky(args: list[str], *, cwd: str | None = None) -> str:
        # Only the content read fails; ls-tree still reports the path present,
        # so this is unmistakably "unreadable" rather than "absent".
        if args and args[0] == "show" and args[-1].endswith("thoughts/postmortem/note.md"):
            msg = "git show timed out after 30.0s"
            raise RuntimeError(msg)
        return real_git_cmd(args, cwd=cwd)

    with (
        patch(
            "engram.team.server_hooks.pre_receive._committer_fingerprint",
            return_value=VALID_FP,
        ),
        patch.object(pre_receive, "_git_cmd", side_effect=flaky),
    ):
        code, stderr = run_hook(
            stdin_text=f"{base_sha} {tip_sha} refs/heads/main\n",
            repo_path=str(team_repo),
        )

    assert code != 0, "an unreadable thought file was skipped and the push allowed"
    assert "thought_content_unreadable" in stderr, stderr
