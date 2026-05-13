"""Tests for ``engram daemon`` typer subcommands + ``engram serve --no-daemon``.

End-to-end "spawn a real daemon process and connect" paths live in
``tests/test_phase5_cli_smoke.py`` (subprocess-based smokes against the
installed binary).
"""

from __future__ import annotations

import json
import os
import re
import socket as socket_module
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from engram.cli import app
from engram.cli.daemon import (
    _build_not_running_status,
    _build_running_status,
    _format_status_text,
    _pid_alive,
)
from engram.config.models import (
    AggregatorConfig,
    DaemonConfig,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
)
from engram.daemon.socket_paths import resolve_paths
from engram.daemon.state import DaemonState, write_state

# Typer/Rich renders help output with ANSI escapes when a tty is detected.
# CliRunner's captured stdout includes those escapes on CI runners where
# the env reports a real terminal. Strip them before substring matches so
# the assertions stay portable.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NO_COLOR_ENV = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ----- fixtures ------------------------------------------------------


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """Short-path vault so UDS socket fits the 104-byte limit on macOS."""
    with tempfile.TemporaryDirectory(prefix="eng-fcli-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        (vault / "thoughts").mkdir(parents=True)
        (vault / ".indexes").mkdir(parents=True)
        # Minimal per-vault config so load_config resolves cleanly.
        (vault / "engram.config.yaml").write_text(
            "vault_name: testvault\nsync:\n  disabled: true\n  auto_pull_on_startup: false\n"
        )
        yield vault


def _effective_config(vault: Path) -> EffectiveConfig:
    return EffectiveConfig(
        default_user="testuser",
        vault_path=vault,
        thoughts_dir=vault / "thoughts",
        index_dir=vault / ".indexes",
        embedding_model="BAAI/bge-small-en-v1.5",
        vault_name="testvault",
        sync=SyncConfig(disabled=True, auto_pull_on_startup=False),
        llm=LLMConfig(),
        aggregator=AggregatorConfig(),
        daemon=DaemonConfig(),
    )


# ----- registration + help --------------------------------------------


def test_engram_daemon_help_lists_four_subcommands() -> None:
    runner = CliRunner(env=_NO_COLOR_ENV)
    result = runner.invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    plain = _plain(result.stdout)
    for sub in ("start", "stop", "status", "logs"):
        assert sub in plain


def test_engram_serve_help_documents_no_daemon_flag() -> None:
    runner = CliRunner(env=_NO_COLOR_ENV)
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--no-daemon" in _plain(result.stdout)


# ----- status: not-running shape -----------------------------------


def test_status_not_running_text(short_vault: Path) -> None:
    config = _effective_config(short_vault)
    payload = _build_not_running_status(config)
    assert payload["daemon"]["running"] is False
    text = _format_status_text(payload)
    assert "not running" in text
    assert "engram daemon start" in text


def test_status_not_running_json_via_cli(short_vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "daemon",
            "status",
            "--config",
            str(short_vault / "engram.config.yaml"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["daemon"]["running"] is False
    assert payload["vault"]["name"] == "testvault"


def test_status_running_includes_pid_and_uptime(short_vault: Path) -> None:
    config = _effective_config(short_vault)
    paths = resolve_paths(short_vault)
    write_state(
        paths.state_file,
        DaemonState(
            pid=99999,
            started_at="2026-05-12T14:20:04+00:00",
            vault_name="testvault",
            vault_path=str(paths.vault),
            hostname=socket_module.gethostname(),
            config_snapshot={},
        ),
    )
    payload = _build_running_status(
        config, {"pid": 99999, "started_at": "2026-05-12T14:20:04+00:00"}
    )
    assert payload["daemon"]["running"] is True
    assert payload["daemon"]["pid"] == 99999
    assert payload["daemon"]["uptime_seconds"] is not None


# ----- stop: not-running path ----------------------------------------


def test_stop_no_state_prints_and_exits_0(short_vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["daemon", "stop", "--config", str(short_vault / "engram.config.yaml")],
    )
    assert result.exit_code == 0
    assert "no daemon running" in result.stdout.lower()


def test_stop_stale_pid_recovers_quietly(short_vault: Path) -> None:
    """If state.json points at a dead PID, stop should not hang/raise."""
    paths = resolve_paths(short_vault)
    write_state(
        paths.state_file,
        DaemonState(
            pid=999999,  # likely-dead PID
            started_at="2026-05-12T14:20:04+00:00",
            vault_name="testvault",
            vault_path=str(paths.vault),
            hostname=socket_module.gethostname(),
            config_snapshot={},
        ),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["daemon", "stop", "--config", str(short_vault / "engram.config.yaml")],
    )
    # Either "already stopped" or normal exit; never a stack trace.
    assert result.exit_code == 0, result.stdout


# ----- logs: not-present path ----------------------------------------


def test_logs_no_log_file_prints_message(short_vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["daemon", "logs", "--config", str(short_vault / "engram.config.yaml")],
    )
    assert result.exit_code == 0
    assert "no log file" in result.stdout.lower()


def test_logs_prints_last_n_lines(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    lines = [f"line {i}\n" for i in range(50)]
    paths.log_file.write_text("".join(lines))
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "daemon",
            "logs",
            "--config",
            str(short_vault / "engram.config.yaml"),
            "--tail",
            "5",
        ],
    )
    assert result.exit_code == 0
    # The output contains lines 45..49.
    for i in range(45, 50):
        assert f"line {i}" in result.stdout
    # Earlier lines should not be present.
    assert "line 10" not in result.stdout


# ----- _pid_alive helper --------------------------------------------


def test_pid_alive_for_self_is_true() -> None:
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_for_unlikely_pid_is_false() -> None:
    # PID 0 always returns ESRCH from kill(0); PID a million is almost
    # always free on dev machines.
    assert _pid_alive(999999) is False


# ----- serve --no-daemon dispatch -----------------------------------


def test_serve_no_daemon_flag_present() -> None:
    """Sanity: --no-daemon flag is exposed via CLI introspection."""
    runner = CliRunner(env=_NO_COLOR_ENV)
    result = runner.invoke(app, ["serve", "--help"])
    assert "--no-daemon" in _plain(result.stdout)
