"""Mock-based tests for engram.sync.gitops error classification + parsing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from engram.sync import gitops
from engram.sync.gitops import GitErrorClass


def _cp(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Permission denied (publickey)", GitErrorClass.AUTH),
        ("remote: Authentication failed for 'https://github.com/...'", GitErrorClass.AUTH),
        ("fatal: could not read Username for 'https://github.com'", GitErrorClass.AUTH),
        ("error: fatal: unable to access ... Returned error: 403", GitErrorClass.AUTH),
        ("remote: Repository not found.", GitErrorClass.NETWORK_PERMANENT),
        ("error: Code 404", GitErrorClass.NETWORK_PERMANENT),
        ("ssh: Could not resolve hostname github.com", GitErrorClass.NETWORK_TRANSIENT),
        (
            "ssh: connect to host github.com port 22: Connection timed out",
            GitErrorClass.NETWORK_TRANSIENT,
        ),
        ("Connection refused", GitErrorClass.NETWORK_TRANSIENT),
        ("fatal: server returned 503 Service Unavailable", GitErrorClass.NETWORK_TRANSIENT),
        (
            "Updates were rejected because the remote contains work that you do not have",
            GitErrorClass.NON_FAST_FORWARD,
        ),
        ("error: failed to push some refs to 'origin'", GitErrorClass.NON_FAST_FORWARD),
        ("CONFLICT (content): Merge conflict in foo.md", GitErrorClass.CONFLICT),
        (
            "Automatic merge failed; fix conflicts and then commit the result.",
            GitErrorClass.CONFLICT,
        ),
        (
            "fatal: Unable to create '/tmp/repo/.git/index.lock': File exists.",
            GitErrorClass.LOCK_HELD,
        ),
        ("Another git process seems to be running in this repository", GitErrorClass.LOCK_HELD),
        ("", GitErrorClass.OK),
        ("totally unrecognized error blob", GitErrorClass.UNKNOWN),
    ],
)
def test_classify_stderr(stderr: str, expected: GitErrorClass) -> None:
    assert gitops.classify_stderr(stderr) is expected


def test_auth_takes_priority_over_network_transient() -> None:
    # 401 + transient cue should still classify as AUTH.
    blob = "fatal: server: 401 Unauthorized; failed to send request"
    assert gitops.classify_stderr(blob) is GitErrorClass.AUTH


def test_network_permanent_takes_priority_over_transient() -> None:
    blob = "remote: Repository not found.\nfatal: failed to send request"
    assert gitops.classify_stderr(blob) is GitErrorClass.NETWORK_PERMANENT


@pytest.mark.asyncio
async def test_is_inside_work_tree_true() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(stdout="true\n")):
        assert await gitops.is_inside_work_tree(Path("/tmp")) is True


@pytest.mark.asyncio
async def test_is_inside_work_tree_false_when_not_repo() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(returncode=128, stderr="not a repo")):
        assert await gitops.is_inside_work_tree(Path("/tmp")) is False


@pytest.mark.asyncio
async def test_current_branch_returns_name() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(stdout="main\n")):
        assert await gitops.current_branch(Path("/x")) == "main"


@pytest.mark.asyncio
async def test_current_branch_detached_returns_none() -> None:
    with patch(
        "engram.sync.gitops.run_git", return_value=_cp(returncode=128, stderr="not symbolic ref")
    ):
        assert await gitops.current_branch(Path("/x")) is None


@pytest.mark.asyncio
async def test_remote_url_strips_whitespace() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(stdout="git@example.com:foo.git\n")):
        assert await gitops.remote_url(Path("/x")) == "git@example.com:foo.git"


@pytest.mark.asyncio
async def test_remote_url_missing_returns_none() -> None:
    with patch(
        "engram.sync.gitops.run_git", return_value=_cp(returncode=2, stderr="No such remote")
    ):
        assert await gitops.remote_url(Path("/x")) is None


@pytest.mark.asyncio
async def test_default_remote_branch_strips_remote_prefix() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(stdout="origin/main\n")):
        assert await gitops.default_remote_branch(Path("/x")) == "main"


@pytest.mark.asyncio
async def test_default_remote_branch_missing_returns_none() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(returncode=128, stderr="missing")):
        assert await gitops.default_remote_branch(Path("/x")) is None


@pytest.mark.asyncio
async def test_git_version_parses_2_43() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(stdout="git version 2.43.0\n")):
        assert await gitops.git_version(Path("/x")) == (2, 43, 0)


@pytest.mark.asyncio
async def test_git_version_unparseable_returns_zeros() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(stdout="garbage")):
        assert await gitops.git_version(Path("/x")) == (0, 0, 0)


@pytest.mark.asyncio
async def test_status_porcelain_z_parses_simple_modified_row() -> None:
    # Two NUL-separated rows: one modified, one new file.
    blob = " M src/foo.py\x00?? src/bar.py\x00"
    with patch("engram.sync.gitops.run_git", return_value=_cp(stdout=blob)):
        entries = await gitops.status_porcelain(Path("/x"))
    assert len(entries) == 2
    assert entries[0].index_status == " "
    assert entries[0].worktree_status == "M"
    assert entries[0].path == "src/foo.py"
    assert entries[1].index_status == "?"
    assert entries[1].path == "src/bar.py"


@pytest.mark.asyncio
async def test_ahead_behind_count_parses_two_ints() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(stdout="2\t5\n")):
        ahead, behind = await gitops.ahead_behind_count(Path("/x"), "main")
    # rev-list --left-right produces "<left> <right>"; we treat right as ahead, left as behind.
    assert (ahead, behind) == (5, 2)


@pytest.mark.asyncio
async def test_ahead_behind_count_no_upstream_returns_zero_zero() -> None:
    with patch("engram.sync.gitops.run_git", return_value=_cp(returncode=128, stderr="bad ref")):
        assert await gitops.ahead_behind_count(Path("/x"), "main") == (0, 0)


@pytest.mark.asyncio
async def test_push_classifies_non_fast_forward() -> None:
    blob = "Updates were rejected because the remote contains work that you do not have"
    with patch(
        "engram.sync.gitops.run_git",
        return_value=_cp(returncode=1, stderr=blob),
    ):
        result = await gitops.push(Path("/x"), "origin", "main")
    assert result.error_class is GitErrorClass.NON_FAST_FORWARD


@pytest.mark.asyncio
async def test_pull_rebase_classifies_conflict() -> None:
    blob = (
        "CONFLICT (content): Merge conflict in foo.md\n"
        "Automatic merge failed; fix conflicts and then commit the result."
    )
    with patch(
        "engram.sync.gitops.run_git",
        return_value=_cp(returncode=1, stderr=blob),
    ):
        result = await gitops.pull_rebase(Path("/x"), "origin", "main")
    assert result.error_class is GitErrorClass.CONFLICT


@pytest.mark.asyncio
async def test_fetch_classifies_auth() -> None:
    with patch(
        "engram.sync.gitops.run_git",
        return_value=_cp(returncode=128, stderr="Permission denied (publickey)"),
    ):
        result = await gitops.fetch(Path("/x"))
    assert result.error_class is GitErrorClass.AUTH
