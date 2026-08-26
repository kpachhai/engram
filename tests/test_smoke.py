"""Bootstrap smoke tests: package imports cleanly, version is well-formed, CLI runs."""

from __future__ import annotations

import re
from importlib.metadata import version as distribution_version

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


def test_package_metadata_matches_dunder_version() -> None:
    """The two homes of the version string must agree.

    ``pyproject.toml`` names the version the wheel is built and published
    under; ``engram/__init__.py`` names the version every CLI command
    reports. They are bumped by two separate edits, so nothing but this
    comparison stops a release that publishes one number and reports the
    other. A ``PackageNotFoundError`` here is a real failure, not a reason
    to skip: it means the suite is running against something other than the
    installed package.
    """
    installed = distribution_version("engram-mcp-server")
    assert installed == engram.__version__, (
        f"distribution metadata says {installed!r} but engram.__version__ says "
        f"{engram.__version__!r}; bump pyproject.toml and src/engram/__init__.py together"
    )
