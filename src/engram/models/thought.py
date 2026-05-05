"""Runtime Thought objects exposed via MCP and used by the storage layer.

A :class:`Thought` is the union of frontmatter fields plus the markdown body
content and the vault-relative file path. :class:`ThoughtWithSimilarity` adds
a cosine similarity score for search results.

Both models include the optional ``vault`` and ``legacy_id`` fields even
though Phase 1 always sets ``vault="default"`` and leaves ``legacy_id`` unset
unless populated by the migration command. Carrying these in v1.0 outputs is
the forward-compat commitment from ``docs/PHASE_1_PLAN.md`` Risk R29.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from engram.models.frontmatter import Portability


class Thought(BaseModel):
    """A captured thought: frontmatter + body content + vault-relative path."""

    model_config = ConfigDict(
        extra="ignore",
        validate_assignment=True,
    )

    id: UUID
    schema_version: int = 1
    prefix: str
    portability: Portability
    source: str
    created_at: datetime
    updated_at: datetime
    fingerprint: str
    tags: list[str] = Field(default_factory=list)
    vault: str = "default"
    legacy_id: str | None = None
    #: Phase 4: GPG primary-key fingerprint (40 hex; canonical form) of the
    #: capturing user when the thought lands in a team-write vault. None for
    #: personal-vault captures (Phase 1+2+3 thoughts; backwards compatible).
    captured_by: str | None = None
    content: str
    file_path: Path


class ThoughtWithSimilarity(Thought):
    """A search-result thought: a :class:`Thought` plus a cosine similarity score (0..1)."""

    similarity: float = Field(ge=0.0, le=1.0)


__all__ = ["Thought", "ThoughtWithSimilarity"]
