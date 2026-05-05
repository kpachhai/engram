"""Tests for engram.utils.run_command - safe subprocess wrapper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from engram.utils.run_command import GIT_NON_INTERACTIVE_ENV, run, run_git


def test_run_simple_command():
    result = run([sys.executable, "-c", "print('hello')"])
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_run_propagates_nonzero_with_check_true():
    with pytest.raises(subprocess.CalledProcessError):
        run([sys.executable, "-c", "import sys; sys.exit(7)"])


def test_run_returns_nonzero_with_check_false():
    result = run([sys.executable, "-c", "import sys; sys.exit(7)"], check=False)
    assert result.returncode == 7


def test_run_rejects_non_sequence_args():
    with pytest.raises(TypeError, match="sequence"):
        run("echo hello")


def test_run_rejects_non_string_arg():
    with pytest.raises(TypeError, match="strings"):
        run(["echo", 42])  # type: ignore[list-item]


def test_run_does_not_interpret_shell_metacharacters():
    """Args are passed verbatim - no shell interpretation."""
    # ; in arg should be a literal char, not a command separator.
    result = run([sys.executable, "-c", "print('a;ls /etc')"])
    assert result.returncode == 0
    assert "a;ls /etc" in result.stdout


def test_run_respects_cwd(tmp_path: Path):
    result = run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path)
    assert str(tmp_path.resolve()) in Path(result.stdout.strip()).resolve().as_posix()


def test_run_respects_timeout():
    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.1)


def test_run_respects_env():
    result = run(
        [sys.executable, "-c", "import os; print(os.environ.get('CUSTOM_VAR', 'unset'))"],
        env={"CUSTOM_VAR": "set-by-test", "PATH": "/usr/bin:/bin"},
    )
    assert "set-by-test" in result.stdout


# === run_git ===


def test_run_git_constructs_git_command():
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.append(args)
        captured_args.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    with patch("engram.utils.run_command.subprocess.run", side_effect=fake_run):
        run_git(["status"], cwd=Path.cwd())

    assert captured_args[0] == ["git", "status"]


def test_run_git_applies_non_interactive_env():
    captured_kwargs = {}

    def fake_run(args, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    with patch("engram.utils.run_command.subprocess.run", side_effect=fake_run):
        run_git(["status"], cwd=Path.cwd())

    env = captured_kwargs["env"]
    for key, value in GIT_NON_INTERACTIVE_ENV.items():
        assert env[key] == value, f"missing or wrong env var: {key}"


def test_run_git_extra_env_overrides_defaults():
    captured_kwargs = {}

    def fake_run(args, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    with patch("engram.utils.run_command.subprocess.run", side_effect=fake_run):
        run_git(["status"], cwd=Path.cwd(), extra_env={"GIT_TERMINAL_PROMPT": "0", "MY_FLAG": "1"})

    env = captured_kwargs["env"]
    assert env["MY_FLAG"] == "1"
    # Defaults still present.
    assert env["GIT_MERGE_AUTOEDIT"] == "no"


def test_run_git_default_timeout():
    captured_kwargs = {}

    def fake_run(args, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    with patch("engram.utils.run_command.subprocess.run", side_effect=fake_run):
        run_git(["status"], cwd=Path.cwd())

    assert captured_kwargs["timeout"] == 30.0
