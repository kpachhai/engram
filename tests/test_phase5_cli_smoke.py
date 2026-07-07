"""Hermetic CLI smoke for ``engram daemon`` + ``engram serve --no-daemon``.

Each test spawns the installed ``engram`` binary via subprocess and
asserts observable state (filesystem layout, exit codes, stdout
contents). Per the engram CLAUDE.md "test the binary, not just the
suite" discipline.

The full proxy-mode round-trip (proxy spawns daemon, sends MCP
initialize, gets response) is gated by operational dogfood because
it requires real FastEmbed model loading per spawn — ~2 s x every
smoke would balloon test wall-clock. The CLI-shape smokes below
verify wiring; the unit/integration tests in ``tests/daemon/`` +
``tests/integration/`` verify the per-connection dispatch contract.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def _engram_bin() -> str:
    binary = shutil.which("engram")
    if binary is None:
        pytest.skip("engram binary not on PATH; run `uv sync` then `uv pip install -e .`")
    return binary


def _smoke_env() -> dict[str, str]:
    return {
        **os.environ,
        "COLUMNS": "200",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    expect_zero: bool = True,
    input_str: str | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - cwd-controlled
        [_engram_bin(), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=_smoke_env(),
        input=input_str,
    )
    if expect_zero and result.returncode != 0:
        msg = (
            f"engram {' '.join(args)} failed (rc={result.returncode}). "
            f"stderr: {result.stderr!r}\n"
            f"stdout: {result.stdout!r}"
        )
        raise AssertionError(msg)
    return result


@pytest.fixture
def smoke_vault() -> Iterator[Path]:
    """Short-path vault with a minimal engram.config.yaml."""
    with tempfile.TemporaryDirectory(prefix="eng-smk5-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        (vault / "thoughts").mkdir(parents=True)
        (vault / ".indexes").mkdir(parents=True)
        (vault / "engram.config.yaml").write_text(
            "vault_name: smoke\nsync:\n  disabled: true\n  auto_pull_on_startup: false\n"
        )
        yield vault


# ----- daemon subcommand registration + help ------------------------


def test_engram_daemon_help_lists_subcommands() -> None:
    """``engram daemon --help`` advertises all 4 subcommands."""
    result = _run(["daemon", "--help"])
    for sub in ("start", "stop", "status", "logs"):
        assert sub in result.stdout, f"daemon subcommand {sub} missing from --help"


def test_engram_daemon_start_help_lists_options() -> None:
    """``engram daemon start --help`` exposes the operator-facing flags."""
    result = _run(["daemon", "start", "--help"])
    for option in ("--vault-path", "--detach", "--force", "--skip-probes"):
        assert option in result.stdout, f"daemon start option {option} missing"


def test_engram_serve_help_documents_no_daemon() -> None:
    """``engram serve --help`` documents the ``--no-daemon`` escape hatch."""
    result = _run(["serve", "--help"])
    assert "--no-daemon" in result.stdout


# ----- delete subcommand --------------------------------------------


def test_engram_delete_help_exposes_dry_run_and_yes() -> None:
    """``engram delete --help`` documents both safety flags."""
    result = _run(["delete", "--help"])
    assert "--dry-run" in result.stdout
    assert "--yes" in result.stdout


def test_engram_delete_invalid_uuid_exits_2(smoke_vault: Path) -> None:
    """``engram delete not-a-uuid`` exits 2 with a clear message on stderr."""
    result = _run(
        [
            "delete",
            "not-a-uuid",
            "--config",
            str(smoke_vault / "engram.config.yaml"),
        ],
        expect_zero=False,
    )
    assert result.returncode == 2
    assert "invalid uuid" in result.stderr.lower()


def test_engram_delete_unknown_id_exits_1(smoke_vault: Path) -> None:
    """``engram delete <unknown-uuid>`` exits 1 with a ``not found`` message."""
    # Need a valid UUID format that just doesn't exist in the vault.
    result = _run(
        [
            "delete",
            "00000000-0000-0000-0000-000000000000",
            "--config",
            str(smoke_vault / "engram.config.yaml"),
        ],
        expect_zero=False,
    )
    assert result.returncode == 1
    assert "not found" in result.stderr.lower()


def _write_serve_lock(smoke_vault: Path, *, pid: int = 99999) -> Path:
    """Drop a synthetic serve-lock marker into the vault for refusal tests."""
    lock_path = smoke_vault / ".indexes" / "engram.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": pid,
                "hostname": "test-host",
                "acquired_at": "2026-05-18T00:00:00+00:00",
                "version": "1",
            }
        ),
        encoding="utf-8",
    )
    return lock_path


def test_engram_delete_refuses_when_serve_lock_held(smoke_vault: Path) -> None:
    """``engram delete`` refuses (exit 2) if the per-vault serve lock is held."""
    _write_serve_lock(smoke_vault)
    result = _run(
        [
            "delete",
            "00000000-0000-0000-0000-000000000000",
            "--config",
            str(smoke_vault / "engram.config.yaml"),
        ],
        expect_zero=False,
    )
    assert result.returncode == 2
    assert "vault lock" in result.stderr.lower()
    assert "pid=99999" in result.stderr


def test_engram_reindex_refuses_when_serve_lock_held(smoke_vault: Path) -> None:
    """``engram reindex`` refuses (exit 2) if the per-vault serve lock is held."""
    _write_serve_lock(smoke_vault)
    result = _run(
        [
            "reindex",
            "--config",
            str(smoke_vault / "engram.config.yaml"),
        ],
        expect_zero=False,
    )
    assert result.returncode == 2
    assert "vault lock" in result.stderr.lower()
    assert "pid=99999" in result.stderr


# ----- status / stop / logs on a cold vault -------------------------


def test_engram_daemon_status_not_running_text(smoke_vault: Path) -> None:
    """``engram daemon status`` against a cold vault exits 0 with not-running."""
    result = _run(["daemon", "status", "--config", str(smoke_vault / "engram.config.yaml")])
    assert "not running" in result.stdout.lower()


def test_engram_daemon_status_not_running_json(smoke_vault: Path) -> None:
    """``engram daemon status --json`` against a cold vault: valid JSON, daemon.running=false."""
    result = _run(
        [
            "daemon",
            "status",
            "--config",
            str(smoke_vault / "engram.config.yaml"),
            "--json",
        ]
    )
    payload = json.loads(result.stdout)
    assert payload["daemon"]["running"] is False
    assert payload["vault"]["name"] == "smoke"
    assert payload["socket"]["present"] is False


def test_engram_daemon_stop_no_daemon_running(smoke_vault: Path) -> None:
    """``engram daemon stop`` against a cold vault prints + exits 0."""
    result = _run(["daemon", "stop", "--config", str(smoke_vault / "engram.config.yaml")])
    assert "no daemon running" in result.stdout.lower()


def test_engram_daemon_logs_no_file(smoke_vault: Path) -> None:
    """``engram daemon logs`` against a vault with no log file: clean message + exit 0."""
    result = _run(["daemon", "logs", "--config", str(smoke_vault / "engram.config.yaml")])
    assert "no log file" in result.stdout.lower()


# ----- daemon detach + stop round-trip ------------------------------


def test_engram_daemon_start_detach_then_stop(smoke_vault: Path) -> None:
    """``engram daemon start --detach`` produces a running daemon; ``stop`` cleans up.

    This is the lightest possible end-to-end test of the daemon
    lifecycle: spawn the binary in detached mode (no readiness pipe;
    we poll the state file instead), confirm the state file appears,
    then issue ``stop`` and confirm it disappears.

    Skipped if the daemon does not become ready within 30s — the
    runtime initialization downloads FastEmbed on first run.
    """
    import time

    config_path = smoke_vault / "engram.config.yaml"

    # Detach returns immediately; the daemon process keeps running.
    _run(
        ["daemon", "start", "--detach", "--config", str(config_path), "--skip-probes"],
        timeout=60.0,
    )
    indexes = smoke_vault / ".indexes"
    state_file = indexes / "engram.state.json"

    # Poll for the state file appearing (proxy of "daemon is ready").
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if state_file.exists():
            break
        time.sleep(0.5)
    else:
        # Best-effort cleanup before failing.
        _run(["daemon", "stop", "--force", "--config", str(config_path)], expect_zero=False)
        pytest.skip(
            "daemon did not become ready within 60s — likely first-run FastEmbed "
            "model download. Re-run after the model is cached."
        )

    try:
        # Verify status reports the daemon as running.
        status = _run(["daemon", "status", "--config", str(config_path), "--json"])
        payload = json.loads(status.stdout)
        assert payload["daemon"]["running"] is True
        assert payload["socket"]["present"] is True
    finally:
        _run(
            ["daemon", "stop", "--force", "--config", str(config_path)],
            timeout=15.0,
        )
        # Give the kernel a beat to unlink the socket file.
        time.sleep(0.5)
        assert not state_file.exists(), "state file should be cleaned after stop"


def test_engram_daemon_sigterm_with_active_connections_exits_promptly(
    smoke_vault: Path,
) -> None:
    """SIGTERM to a daemon with active UDS connections must exit in <5s.

    Reproduces the 60s+ ``engram daemon stop`` hang observed on macOS:
    when the daemon has multiple idle UDS connections registered with
    its kqueue selector, an externally delivered SIGTERM was deferred
    until an unrelated event (proxy keepalive ~60s later) woke the
    selector. Pre-fix: SIGTERM is processed after ~60-70s. Post-fix:
    SIGTERM is processed within ~1s thanks to the periodic-wakeup pump
    plus a synchronous-handler fallback in
    :meth:`engram.daemon.server.DaemonServer._install_async_signal_handlers`.
    """
    import signal
    import socket as socket_mod
    import time

    config_path = smoke_vault / "engram.config.yaml"

    _run(
        ["daemon", "start", "--detach", "--config", str(config_path), "--skip-probes"],
        timeout=60.0,
    )
    indexes = smoke_vault / ".indexes"
    state_file = indexes / "engram.state.json"
    socket_path = indexes / "engram.sock"

    # Poll for readiness (state file appears AFTER bind).
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if state_file.exists() and socket_path.exists():
            break
        time.sleep(0.2)
    else:
        _run(["daemon", "stop", "--force", "--config", str(config_path)], expect_zero=False)
        pytest.skip(
            "daemon did not become ready within 60s — likely first-run FastEmbed "
            "model download. Re-run after the model is cached."
        )

    sockets: list[socket_mod.socket] = []
    try:
        state_data = json.loads(state_file.read_text())
        daemon_pid = int(state_data["pid"])

        # Open 4 concurrent UDS connections (matches the maintainer-reported
        # repro: 4 idle Claude Code proxy sessions).
        for _ in range(4):
            sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            sock.connect(str(socket_path))
            sockets.append(sock)

        # Brief settle so accept() runs and each connection is tracked.
        time.sleep(0.3)

        # Send SIGTERM directly; time how long the daemon takes to exit.
        started_at = time.monotonic()
        os.kill(daemon_pid, signal.SIGTERM)

        # Poll for pid exit. Generous 5s budget: pre-fix this would hit ~60s.
        exited = False
        while time.monotonic() - started_at < 5.0:
            try:
                os.kill(daemon_pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                exited = True
                break
        duration = time.monotonic() - started_at

        assert exited, (
            f"daemon (pid {daemon_pid}) did not exit within 5s of SIGTERM "
            f"({duration:.2f}s elapsed); regression of the proxy-connection "
            "stop-hang bug"
        )
        assert duration < 5.0, (
            f"SIGTERM-to-exit took {duration:.2f}s; expected <5s with the "
            "periodic-wakeup pump + synchronous-handler fallback"
        )
    finally:
        import contextlib as _contextlib

        for sock in sockets:
            with _contextlib.suppress(OSError):
                sock.close()
        # Belt-and-suspenders cleanup if assertion fired and daemon survived.
        _run(
            ["daemon", "stop", "--force", "--config", str(config_path)],
            expect_zero=False,
            timeout=15.0,
        )


# ----- daemon logs --tail bounds ------------------------------------


def test_daemon_logs_tail_zero_prints_nothing(smoke_vault: Path) -> None:
    """--tail 0 must print nothing, not the whole log (log may carry PII)."""
    log_file = smoke_vault / ".indexes" / "engram.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("line-1\nline-2\nline-3\n")
    result = _run(
        [
            "daemon",
            "logs",
            "--tail",
            "0",
            "--config",
            str(smoke_vault / "engram.config.yaml"),
        ],
    )
    assert "line-1" not in result.stdout
    assert "line-3" not in result.stdout


def test_daemon_logs_negative_tail_rejected(smoke_vault: Path) -> None:
    """--tail -5 is rejected instead of producing a nonsense slice."""
    log_file = smoke_vault / ".indexes" / "engram.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("line-1\n")
    result = _run(
        [
            "daemon",
            "logs",
            "--tail",
            "-5",
            "--config",
            str(smoke_vault / "engram.config.yaml"),
        ],
        expect_zero=False,
    )
    assert result.returncode != 0


# ----- doctor surfaces daemon-mode rows ------------------------------


def test_doctor_emits_daemon_check_rows(smoke_vault: Path) -> None:
    """`engram doctor` output must include the daemon-mode check family."""
    result = _run(
        ["doctor", "--config", str(smoke_vault / "engram.config.yaml")],
        expect_zero=False,  # model-cache warnings are fine; wiring is the target
    )
    assert "daemon_running" in result.stdout, (
        f"daemon doctor rows absent from `engram doctor` output:\n{result.stdout}"
    )
