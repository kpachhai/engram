"""Phase 3 doc-example sanity tests (Step 22 verifier).

Extracts fenced ``bash`` examples from the Phase 3 docs and asserts:

* Every ``engram <subcommand>`` referenced in an example actually
  exists as a registered subcommand in the typer app.
* No example invokes a destructive operation (``rm -rf``,
  ``git push --force``) that the docs intend to be illustrative
  rather than runnable.

This is the lighter-weight version of the plan's verifier ("run each
bash block against the patched code") - actually executing the
examples would create real vault directories, attempt real API
calls, and time out CI. The contract here is: the docs' commands
match the CLI's surface so a copy-paste of the example uses real
flags.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engram.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

_EXAMPLE_DOCS = (
    DOCS_DIR / "MULTI_VAULT_SETUP.md",
    DOCS_DIR / "FRIEND_SHARE_GUIDE.md",
    DOCS_DIR / "LLM_FEATURES.md",
)

_BASH_BLOCK_RE = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)
_ENGRAM_CMD_RE = re.compile(r"\bengram\s+([a-z][a-z0-9_-]*)\b")


def _registered_subcommands() -> set[str]:
    """Return the set of typer subcommand names registered on the root app."""
    names: set[str] = set()
    for cmd in app.registered_commands:
        if cmd.name:
            names.add(cmd.name)
        elif cmd.callback is not None:
            names.add(cmd.callback.__name__.replace("_cmd", "").replace("_", "-"))
    # Typer also lets you register subcommands without an explicit name; fall
    # back to introspecting registered_groups in case the app structure shifts.
    for group in app.registered_groups:
        if group.name:
            names.add(group.name)
    return names


_REGISTERED = _registered_subcommands()


@pytest.mark.parametrize("md_path", _EXAMPLE_DOCS, ids=lambda p: p.name)
def test_phase3_example_subcommands_exist(md_path: Path) -> None:
    text = md_path.read_text(encoding="utf-8")
    blocks = _BASH_BLOCK_RE.findall(text)
    referenced: set[str] = set()
    for block in blocks:
        for match in _ENGRAM_CMD_RE.findall(block):
            if match in {"engram"}:  # avoid recursion on stray text
                continue
            referenced.add(match)
    missing = referenced - _REGISTERED
    assert not missing, (
        f"{md_path.name} references engram subcommands that are not "
        f"registered: {sorted(missing)} (registered: {sorted(_REGISTERED)})"
    )


@pytest.mark.parametrize("md_path", _EXAMPLE_DOCS, ids=lambda p: p.name)
def test_phase3_examples_no_destructive_commands(md_path: Path) -> None:
    """Doc bash examples should not invoke destructive operations."""
    text = md_path.read_text(encoding="utf-8")
    blocks = _BASH_BLOCK_RE.findall(text)
    forbidden = (
        "rm -rf /",
        "git push --force",
        "git reset --hard origin",
    )
    for block in blocks:
        for pat in forbidden:
            assert pat not in block, (
                f"{md_path.name} bash example contains destructive command: {pat!r}"
            )
