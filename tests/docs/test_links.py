"""Doc link validity test.

Walks the multi-vault docs and asserts every relative markdown link
target resolves to a file on disk.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

#: Multi-vault docs.
_PHASE_3_DOCS = (
    DOCS_DIR / "adr" / "006-multi-vault-and-llm.md",
    DOCS_DIR / "MULTI_VAULT_SETUP.md",
    DOCS_DIR / "FRIEND_SHARE_GUIDE.md",
    DOCS_DIR / "LLM_FEATURES.md",
    DOCS_DIR / "PHASE_3_CODE_COMPLETE.md",
)

#: Match `[text](path)` markdown links; the target group is the path.
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s#]+)(?:#[^)]*)?\)")


def _iter_links(md_path: Path) -> list[tuple[str, int]]:
    """Yield ``(target, line_number)`` for every relative link in ``md_path``."""
    out: list[tuple[str, int]] = []
    for lineno, raw in enumerate(md_path.read_text(encoding="utf-8").splitlines(), 1):
        for match in _LINK_RE.finditer(raw):
            target = match.group(1).strip()
            # Skip absolute URLs and pure anchors.
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            out.append((target, lineno))
    return out


@pytest.mark.parametrize("md_path", _PHASE_3_DOCS, ids=lambda p: p.name)
def test_phase3_doc_links_resolve(md_path: Path) -> None:
    assert md_path.exists(), f"phase3 doc missing: {md_path}"
    links = _iter_links(md_path)
    base_dir = md_path.parent
    missing: list[tuple[str, int]] = []
    for target, line_no in links:
        # Resolve target relative to the markdown file's directory.
        if target.startswith("/"):
            resolved = REPO_ROOT / target.lstrip("/")
        else:
            resolved = (base_dir / target).resolve()
        if not resolved.exists():
            missing.append((target, line_no))
    assert not missing, f"{md_path.name} has dangling link targets: " + ", ".join(
        f"{t}:{ln}" for t, ln in missing
    )
