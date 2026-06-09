"""Archive-move helper for consolidation.

Archiving relocates a superseded original from ``thoughts_dir`` to the
vault's ``archive/`` tree (same relative path). The body bytes are untouched
- only the frontmatter gains ``archived_at`` + ``superseded_by``. Living
outside ``thoughts_dir``, archived files are invisible to reindex, capture,
and the markdown doctor scans, so the index stays curated while the episodic
record remains complete and git-synced.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from engram.errors import ThoughtNotFoundError, VaultError
from engram.storage.markdown import _FRONTMATTER_FENCE_NL, split_frontmatter
from engram.utils.atomic_write import atomic_write_text

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path
    from uuid import UUID


def archive_thought_file(
    *,
    thoughts_dir: Path,
    archive_dir: Path,
    rel_path: str,
    superseded_by: UUID,
    archived_at: datetime,
) -> tuple[Path, Path]:
    """Move ``thoughts_dir/rel_path`` to ``archive_dir/rel_path`` with annotation.

    Returns ``(original_path, archived_path)``. Idempotent for resume: when
    the original is already gone and the archived copy exists (crash after
    the move), the existing archive is returned untouched.

    Raises:
        ThoughtNotFoundError: neither the original nor an archived copy exists.
        VaultError: both the original and an archived copy exist (ambiguous
            state needing operator attention), the file has no frontmatter,
            or ``archived_at`` is timezone-naive.
    """
    if archived_at.tzinfo is None:
        msg = "archived_at must be timezone-aware (UTC)"
        raise VaultError(msg)

    original = thoughts_dir / rel_path
    destination = archive_dir / rel_path

    if original.exists() and destination.exists():
        msg = (
            f"both the live file and an archived copy exist for {rel_path!r}; "
            "resolve manually before re-running consolidate"
        )
        raise VaultError(msg)
    if not original.exists():
        if destination.exists():
            return original, destination
        msg = f"no file to archive at {original}"
        raise ThoughtNotFoundError(msg)

    raw = original.read_text(encoding="utf-8")
    split = split_frontmatter(raw)
    if split is None:
        msg = f"cannot archive {original}: file has no frontmatter fence"
        raise VaultError(msg)
    fm_yaml, body = split

    yaml_rt = YAML(typ="rt")
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    data = yaml_rt.load(fm_yaml)
    data["archived_at"] = DoubleQuotedScalarString(archived_at.isoformat())
    data["superseded_by"] = DoubleQuotedScalarString(str(superseded_by))
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    new_fm = buf.getvalue()

    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        destination,
        f"{_FRONTMATTER_FENCE_NL}{new_fm}{_FRONTMATTER_FENCE_NL}{body}",
    )
    original.unlink()
    return original, destination


__all__ = ["archive_thought_file"]
