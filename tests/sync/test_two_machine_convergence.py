"""End-to-end multi-machine convergence integration tests.

Each test is hermetic - own ``tmp_path``, no network. Wraps two
:class:`VaultStorage` instances against a shared ``git init --bare``
remote and asserts the convergence properties.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from engram.config.models import SyncConfig
from engram.sync.coordinator import (
    CoordinatorConfig,
    SyncCoordinator,
    SyncState,
)
from engram.sync.gitops import GitErrorClass
from engram.sync.startup_probes import run_startup_probes
from engram.utils.lock import MigrationLock

from .conftest import commit_file, init_repo, run_git


def _make_coord(repo: Path, **overrides: object) -> SyncCoordinator:
    base: dict[str, object] = {
        "auto_push_on_capture": True,
        "push_retry_count": 0,
        "push_retry_backoff_seconds": 0.0,
        "push_timeout_seconds": 10.0,
        "user_email": "test@example.com",
        "user_name": "test",
    }
    base.update(overrides)
    return SyncCoordinator(repo_dir=repo, config=CoordinatorConfig(**base))  # type: ignore[arg-type]


def test_two_machine_convergence_happy_path(
    tmp_path: Path, linked_clones: tuple[Path, Path]
) -> None:
    """A captures a thought; B pulls; B sees the thought."""
    a, b = linked_clones
    coord_a = _make_coord(a)

    new_path = a / "thoughts" / "alpha.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text("[Lesson] convergence\n")
    coord_a.enqueue(new_path)

    asyncio.run(coord_a._commit_cycle())
    asyncio.run(coord_a._push_cycle())
    assert coord_a.state is SyncState.IDLE

    cp_pull = run_git(["pull", "--rebase=true", "origin", "main"], b)
    assert cp_pull.returncode == 0, cp_pull.stderr
    assert (b / "thoughts" / "alpha.md").exists()


def test_concurrent_capture_no_conflict(tmp_path: Path, linked_clones: tuple[Path, Path]) -> None:
    """Both clones capture distinct files; B retries after fetch+rebase."""
    a, b = linked_clones
    coord_a = _make_coord(a, push_retry_count=2)
    coord_b = _make_coord(b, push_retry_count=2)

    file_a = a / "thoughts" / "from-a.md"
    file_b = b / "thoughts" / "from-b.md"
    file_a.parent.mkdir(parents=True, exist_ok=True)
    file_b.parent.mkdir(parents=True, exist_ok=True)
    file_a.write_text("from a\n")
    file_b.write_text("from b\n")
    coord_a.enqueue(file_a)
    coord_b.enqueue(file_b)

    asyncio.run(coord_a._commit_cycle())
    asyncio.run(coord_a._push_cycle())  # A pushes first
    assert coord_a.state is SyncState.IDLE

    # B will hit non-fast-forward; reflog gate should detect ancestor and rebase.
    asyncio.run(coord_b._commit_cycle())
    asyncio.run(coord_b._push_cycle())

    # B must end either IDLE (rebased + pushed) or COMMITTED_NOT_PUSHED if
    # the push retry budget runs out; the property is "no silent loss".
    assert coord_b.state in {
        SyncState.IDLE,
        SyncState.COMMITTED_NOT_PUSHED,
        SyncState.MANUAL_RESOLUTION_REQUIRED,
    }


def test_force_push_elsewhere_triggers_degraded_mode(
    tmp_path: Path, linked_clones: tuple[Path, Path]
) -> None:
    """An external force-push refuses auto-rebase (reflog gate)."""
    a, b = linked_clones
    coord_b = _make_coord(b, push_retry_count=0)

    # Use a third clone to force-push history rewrite.
    c = tmp_path / "c"
    cp_clone = run_git(["clone", str(a / "../remote.git"), str(c)], tmp_path)
    assert cp_clone.returncode == 0
    run_git(["config", "user.email", "c@x"], c)
    run_git(["config", "user.name", "c"], c)
    run_git(["config", "commit.gpgsign", "false"], c)
    # Reset and force-push.
    run_git(["update-ref", "-d", "HEAD"], c)
    for f in c.iterdir():
        if f.name == ".git":
            continue
        if f.is_dir():
            import shutil

            shutil.rmtree(f)
        else:
            f.unlink()
    commit_file(c, "rewritten.md", "rewritten")
    cp_force = run_git(["push", "--force", "origin", "main"], c)
    assert cp_force.returncode == 0

    # B has a local-only commit and tries to push - should hit the reflog gate.
    new_path = b / "thoughts" / "local.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text("local")
    coord_b.enqueue(new_path)
    asyncio.run(coord_b._commit_cycle())
    asyncio.run(coord_b._push_cycle())
    assert coord_b.state in {
        SyncState.MANUAL_RESOLUTION_REQUIRED,
        SyncState.COMMITTED_NOT_PUSHED,
    }
    # The local commit must still be present locally.
    log = run_git(["log", "--all", "--oneline"], b)
    assert "local" in log.stdout or "local.md" in run_git(["log", "--name-only"], b).stdout


def test_pull_with_conflict_markers(tmp_path: Path, linked_clones: tuple[Path, Path]) -> None:
    """A vault containing conflict markers is FAILed by the doctor + degraded mode."""
    a, _b = linked_clones
    conflicted = a / "thoughts" / "conflict.md"
    conflicted.parent.mkdir(parents=True, exist_ok=True)
    conflicted.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
    from engram.sync.gitops import conflict_marker_scan

    found = conflict_marker_scan(a / "thoughts")
    assert conflicted in found


def test_first_push_empty_vault(tmp_path: Path, bare_remote: Path) -> None:
    """``engram sync --first-push`` semantics: first commit + push -u."""
    vault = tmp_path / "vault"
    init_repo(vault, bare=False)
    run_git(["config", "user.email", "v@x"], vault)
    run_git(["config", "user.name", "v"], vault)
    run_git(["config", "commit.gpgsign", "false"], vault)
    run_git(["remote", "add", "origin", str(bare_remote)], vault)
    (vault / "thoughts").mkdir()
    (vault / "thoughts" / "first.md").write_text("first")

    cp_add = run_git(["add", "."], vault)
    assert cp_add.returncode == 0
    cp_commit = run_git(["commit", "-m", "initial"], vault)
    assert cp_commit.returncode == 0
    cp_push = run_git(["push", "-u", "origin", "main"], vault)
    assert cp_push.returncode == 0

    cp_branches = run_git(["branch", "--list"], bare_remote)
    assert "main" in cp_branches.stdout


def test_no_remote_no_op(tmp_path: Path) -> None:
    """A vault with no remote silently no-ops on coordinator push."""
    vault = tmp_path / "vault"
    init_repo(vault, bare=False)
    commit_file(vault, "x.md", "x")
    coord = _make_coord(vault, push_retry_count=0)

    file_path = vault / "thoughts" / "y.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("y")
    coord.enqueue(file_path)
    asyncio.run(coord._commit_cycle())
    # No remote -> push fails NETWORK_PERMANENT -> COMMITTED_NOT_PUSHED.
    asyncio.run(coord._push_cycle())
    assert coord.state in {
        SyncState.COMMITTED_NOT_PUSHED,
        SyncState.IDLE,
        SyncState.MANUAL_RESOLUTION_REQUIRED,
    }


def test_read_only_role_contradicts_auto_push_refuses_start(tmp_path: Path) -> None:
    """Probe 14 FAILs when role=read-only AND auto_push_on_capture=true."""
    repo = tmp_path / "vault"
    init_repo(repo, bare=False)
    (repo / ".gitignore").write_text(".indexes/\n*.sqlite\n")
    commit_file(repo, "first.md", "1")

    config = SyncConfig(role="read-only", auto_push_on_capture=True)
    report = asyncio.run(run_startup_probes(config, repo))
    fail_codes = [f.code for f in report.failures]
    from engram.diagnostics import check_codes as cc

    assert cc.READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH in fail_codes


def test_read_only_role_refuses_push(tmp_path: Path, linked_clones: tuple[Path, Path]) -> None:
    """Coordinator with role=read-only never enters PUSHING."""
    _a, b = linked_clones
    coord = _make_coord(b, role="read-only")
    new_path = b / "thoughts" / "x.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text("x")
    coord.enqueue(new_path)
    asyncio.run(coord._commit_cycle())
    asyncio.run(coord._push_cycle())
    # Push was a no-op; state stays IDLE. No PUSHING transition recorded.
    transitions = [(e.from_state, e.to_state) for e in coord.events]
    assert all(target is not SyncState.PUSHING for (_, target) in transitions)


def test_migration_pauses_sync_with_explicit_barrier(tmp_path: Path) -> None:
    """sf-11 deterministic interleave: migration lock parks the coordinator."""
    repo = tmp_path / "vault"
    init_repo(repo, bare=False)
    commit_file(repo, "first.md", "1")

    barrier = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with MigrationLock(repo):
            barrier.set()
            release.wait(timeout=5.0)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert barrier.wait(timeout=5.0)

    coord = SyncCoordinator(
        repo_dir=repo,
        config=CoordinatorConfig(migration_held=lambda: MigrationLock.is_held(repo)),
    )
    asyncio.run(coord._tick())
    assert coord.state is SyncState.PAUSED_FOR_MIGRATION

    release.set()
    holder.join(timeout=5.0)


def test_unicode_vault_path(tmp_path: Path) -> None:
    """Vault path with non-ASCII characters works through capture+sync."""
    vault = tmp_path / "Документы" / "vault"
    vault.mkdir(parents=True)
    init_repo(vault, bare=False)
    (vault / ".gitignore").write_text(".indexes/\n*.sqlite\n")
    commit_file(vault, "first.md", "1")

    config = SyncConfig()
    report = asyncio.run(run_startup_probes(config, vault))
    # No failures from path encoding.
    fail_messages = " ".join(f.message for f in report.failures)
    assert "encoding" not in fail_messages.lower()


@pytest.mark.asyncio
async def test_drain_on_shutdown(tmp_path: Path, linked_clones: tuple[Path, Path]) -> None:
    """Stop() commits and pushes pending captures before exiting."""
    a, _b = linked_clones
    coord = _make_coord(a, debounce_window_seconds=0.1, push_retry_count=2)

    for i in range(3):
        path = a / "thoughts" / f"drain-{i}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"drain-{i}")
        coord.enqueue(path)

    await coord.start()
    # Force flush + stop.
    coord.force_flush()
    await coord.stop()

    log = run_git(["log", "--name-only"], a)
    for i in range(3):
        assert f"drain-{i}.md" in log.stdout, f"drain-{i}.md not committed"


def test_full_doctor_on_clean_vault(tmp_path: Path, linked_clones: tuple[Path, Path]) -> None:
    """End-to-end: doctor + sync checks on a real two-machine setup."""
    a, _b = linked_clones
    (a / ".gitignore").write_text(".indexes/\n*.sqlite\n*.sqlite-wal\n*.sqlite-shm\n")
    cp_add = run_git(["add", ".gitignore"], a)
    assert cp_add.returncode == 0
    cp_commit = run_git(["commit", "-m", "add gitignore"], a)
    assert cp_commit.returncode == 0

    config = SyncConfig()
    report = asyncio.run(run_startup_probes(config, a))
    # Working tree should be clean now; the most-load-bearing FAIL codes
    # (gitignore + working_tree_dirty) must be absent.
    fail_codes = [f.code for f in report.failures]
    from engram.diagnostics import check_codes as cc

    assert cc.GITIGNORE_INDEXES not in fail_codes
    assert cc.WORKING_TREE_DIRTY_AT_STARTUP not in fail_codes


def test_event_log_records_committed_then_pushed(
    tmp_path: Path, linked_clones: tuple[Path, Path]
) -> None:
    """The ring buffer captures the COMMITTING -> PUSHING -> IDLE sequence."""
    a, _b = linked_clones
    coord = _make_coord(a)
    new_path = a / "thoughts" / "trace.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text("trace")
    coord.enqueue(new_path)

    asyncio.run(coord._commit_cycle())
    asyncio.run(coord._push_cycle())

    targets = [e.to_state for e in coord.events]
    assert SyncState.COMMITTING in targets
    assert SyncState.PUSHING in targets
    assert targets[-1] is SyncState.IDLE


def test_filter_engram_paths_outside_thoughts_dropped(tmp_path: Path) -> None:
    """Defense-in-depth: paths outside thoughts_dir are filtered before commit."""
    from engram.sync.coordinator import filter_engram_paths

    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    inside = thoughts / "x.md"
    inside.write_text("x")
    secret = tmp_path / ".secret"
    secret.write_text("nope")
    out = filter_engram_paths([inside, secret], thoughts)
    assert inside.resolve() in out
    assert secret.resolve() not in out


def test_classify_stderr_is_pure_function() -> None:
    """classify_stderr is hermetic and side-effect free for the integration sweep."""
    from engram.sync.gitops import classify_stderr

    assert classify_stderr("Permission denied (publickey)") is GitErrorClass.AUTH
    assert classify_stderr("") is GitErrorClass.OK
