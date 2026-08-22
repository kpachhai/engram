"""Hermetic CLI smoke for the ``engram doctor`` LLM rows.

The LLM checks were called with no provider and no budget, so both took
their "nothing configured" branch and printed OK for something doctor had
never looked at - and the block was gated on having more than one vault, so
a single-vault install with an LLM configured got no LLM rows at all. Both
are wiring, which only a run of the installed binary can catch.

The configured ``base_url`` is deliberately untrusted, so provider
resolution refuses before any socket is opened.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_LLM_VAULT_CONFIG = """\
vault_name: doctor-smoke
sync:
  disabled: true
  auto_pull_on_startup: false
llm:
  provider: openai_compatible
  base_url: http://untrusted.example.invalid/v1
"""


def _engram_bin() -> str:
    binary = shutil.which("engram")
    if binary is None:
        pytest.skip("engram binary not on PATH; run `uv sync` then `uv pip install -e .`")
    return binary


@pytest.fixture
def llm_vault() -> Iterator[Path]:
    """Single-vault install that configures an LLM provider."""
    with tempfile.TemporaryDirectory(prefix="eng-doc-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        (vault / "thoughts").mkdir(parents=True)
        (vault / ".indexes").mkdir(parents=True)
        (vault / "engram.config.yaml").write_text(_LLM_VAULT_CONFIG, encoding="utf-8")
        yield vault


def _doctor_lines(vault: Path) -> list[str]:
    result = subprocess.run(  # noqa: S603 - fixture-controlled paths
        [_engram_bin(), "doctor", "--config", str(vault / "engram.config.yaml")],
        capture_output=True,
        text=True,
        check=False,
        timeout=120.0,
        env={**os.environ, "COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"},
    )
    assert result.returncode != 2, f"doctor failed outright:\n{result.stdout}\n{result.stderr}"
    return result.stdout.splitlines()


def test_single_vault_install_gets_the_llm_rows(llm_vault: Path) -> None:
    lines = _doctor_lines(llm_vault)
    names = [line for line in lines if "llm_provider_reachable" in line]
    caps = [line for line in lines if "llm_daily_cost_cap_approached" in line]

    assert names, f"no llm_provider_reachable row on a single-vault install:\n{lines}"
    assert caps, f"no llm_daily_cost_cap_approached row on a single-vault install:\n{lines}"


def test_unresolvable_provider_is_not_reported_ok(llm_vault: Path) -> None:
    """The row must not claim a clean result for a provider it never reached."""
    lines = _doctor_lines(llm_vault)
    row = next(line for line in lines if "llm_provider_reachable" in line)

    assert "[WARN]" in row, f"unmeasured provider reported as {row!r}"
