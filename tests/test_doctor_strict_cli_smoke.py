"""Hermetic CLI smoke for ``engram doctor --strict``.

The default exit code counts a skipped row as clean, so on a non-git vault a
run with seventeen rows that never ran exits 0 exactly like a run where every
row passed. ``--strict`` is the opt-in that separates them. Both halves are
wiring - a Typer option that is declared but never reaches ``typer.Exit``
looks identical to one that works - so they are asserted against the
installed binary rather than the in-process function.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_VAULT_CONFIG = """\
vault_name: strict-smoke
sync:
  disabled: true
  auto_pull_on_startup: false
"""


def _engram_bin() -> str:
    binary = shutil.which("engram")
    if binary is None:
        pytest.skip("engram binary not on PATH; run `uv sync` then `uv pip install -e .`")
    return binary


@pytest.fixture
def skipping_vault() -> Iterator[Path]:
    """A vault that is not a git working tree, so the sync rows cannot run."""
    with tempfile.TemporaryDirectory(prefix="eng-strict-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        (vault / "thoughts").mkdir(parents=True)
        (vault / ".indexes").mkdir(parents=True)
        (vault / "engram.config.yaml").write_text(_VAULT_CONFIG, encoding="utf-8")
        yield vault


def _doctor(vault: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    # HOME points at the fixture root so the developer's own
    # ~/.config/engram/config.yaml cannot layer an LLM provider - and its
    # WARN row - underneath the vault config this test wrote.
    return subprocess.run(  # noqa: S603 - fixture-controlled paths
        [_engram_bin(), "doctor", "--config", str(vault / "engram.config.yaml"), *flags],
        capture_output=True,
        text=True,
        check=False,
        timeout=120.0,
        env={
            **os.environ,
            "HOME": str(vault.parent),
            "COLUMNS": "200",
            "NO_COLOR": "1",
            "TERM": "dumb",
        },
    )


def _skipped_count(output: str) -> int:
    return sum(1 for line in output.splitlines() if line.lstrip().startswith("[SKIP]"))


def test_the_flag_is_wired_into_the_command(skipping_vault: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixture-controlled paths
        [_engram_bin(), "doctor", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60.0,
        env={**os.environ, "COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"},
    )
    assert result.returncode == 0, result.stderr
    assert "--strict" in result.stdout


def test_default_exit_code_still_reads_clean_over_skipped_rows(skipping_vault: Path) -> None:
    """The compatibility half: anything branching on the old exit code is unaffected."""
    result = _doctor(skipping_vault)
    assert _skipped_count(result.stdout) > 0, f"no skipped rows to test against:\n{result.stdout}"
    assert result.returncode == 0, f"default mode moved off 0:\n{result.stdout}\n{result.stderr}"
    assert "did not run" in result.stdout


def test_strict_exits_three_and_names_the_count(skipping_vault: Path) -> None:
    result = _doctor(skipping_vault, "--strict")
    skipped = _skipped_count(result.stdout)
    assert skipped > 0, f"no skipped rows to test against:\n{result.stdout}"
    assert result.returncode == 3, f"--strict exited {result.returncode}:\n{result.stdout}"
    assert f"{skipped} of " in result.stdout
    assert "--strict" in result.stdout


def test_strict_reports_the_same_rows_as_the_default_run(skipping_vault: Path) -> None:
    """Only the exit code and the verdict sentence move; the diagnosis does not."""
    plain = _doctor(skipping_vault).stdout.splitlines()
    strict = _doctor(skipping_vault, "--strict").stdout.splitlines()
    rows_plain = [line for line in plain if line.lstrip().startswith("[")]
    rows_strict = [line for line in strict if line.lstrip().startswith("[")]
    # Two empty lists compare equal, so a run that printed nothing at all would
    # satisfy the comparison below without either mode having been exercised.
    assert rows_plain, f"the default run printed no rows to compare:\n{plain}"
    assert rows_plain == rows_strict
