"""Bootstrap smoke tests: package imports cleanly, version is well-formed, CLI runs."""

from __future__ import annotations

import re

from typer.testing import CliRunner

import engram
from engram.cli import app


def test_version_attribute_exists() -> None:
    assert hasattr(engram, "__version__")


def test_version_is_pep440_compliant() -> None:
    # Loose PEP 440 pattern: digits.digits.digits with optional pre/post/dev suffix.
    assert re.match(r"^\d+\.\d+\.\d+([abrc]\d+|\.post\d+|\.dev\d+)?$", engram.__version__)


def test_cli_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert engram.__version__ in result.stdout
