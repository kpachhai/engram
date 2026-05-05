"""MCP tool I/O Pydantic models per ``02-TECHNICAL_DESIGN.md`` MCP API Contract.

Each of the five core tools - ``capture_thought``, ``search_thoughts``,
``list_thoughts``, ``thought_stats``, ``fetch`` - has paired Input and Output
models. Inputs use ``extra="forbid"`` so unknown fields surface as clear
validation errors; outputs use ``extra="ignore"`` so the tool implementation
can pass extra context internally without polluting the wire format.

API stability: per ``02-TECHNICAL_DESIGN.md`` API Stability Commitment, these
shapes are stable for the v1.x lifetime. Only non-breaking additions are
permitted (new optional fields, new tools, new optional filter dimensions).
Breaking changes warrant v2.0.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from engram.models.frontmatter import Portability
from engram.models.thought import Thought, ThoughtWithSimilarity

#: Sort options for ``list_thoughts``.
SortOption = Literal["created_at_desc", "created_at_asc", "updated_at_desc"]


class Filter(BaseModel):
    """Filter clause used by ``search_thoughts`` and ``list_thoughts``.

    For multi-value fields, list-form means OR-within-field; the overall filter
    is AND-across-fields. ``tags`` semantics: match if any listed tag is present
    on the thought. Empty list and absence are treated identically (Q4 default).
    """

    model_config = ConfigDict(extra="forbid")

    prefix: str | list[str] | None = None
    portability: Portability | list[Portability] | None = None
    source: str | list[str] | None = None
    tags: list[str] | None = None
    vault: str | list[str] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


class CaptureInputMetadata(BaseModel):
    """Optional metadata override for ``capture_thought``."""

    model_config = ConfigDict(extra="forbid")

    prefix: str | None = None
    portability: Portability | None = None
    source: str | None = None
    tags: list[str] | None = None


class CaptureInput(BaseModel):
    """Input to ``capture_thought``."""

    model_config = ConfigDict(extra="forbid")

    content: str
    metadata: CaptureInputMetadata | None = None


class CaptureOutput(BaseModel):
    """Output of ``capture_thought``: the freshly captured thought's identity."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    file_path: str
    fingerprint: str


class SearchInput(BaseModel):
    """Input to ``search_thoughts``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    k: int = Field(default=10, ge=1, le=100)
    filter: Filter | None = None


class SearchOutput(BaseModel):
    """Output of ``search_thoughts``."""

    model_config = ConfigDict(extra="ignore")

    results: list[ThoughtWithSimilarity]
    total_found: int


class ListInput(BaseModel):
    """Input to ``list_thoughts``."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=0, le=500)
    offset: int = Field(default=0, ge=0)
    filter: Filter | None = None
    sort: SortOption = "created_at_desc"


class ListOutput(BaseModel):
    """Output of ``list_thoughts``."""

    model_config = ConfigDict(extra="ignore")

    results: list[Thought]
    total_count: int


class PortabilityCounts(BaseModel):
    """Strict shape of the ``by_portability`` field in ``thought_stats``."""

    model_config = ConfigDict(extra="forbid")

    portable: int = 0
    sensitive: int = 0
    block: int = 0


class StatsOutput(BaseModel):
    """Output of ``thought_stats``."""

    model_config = ConfigDict(extra="ignore")

    total_count: int
    by_prefix: dict[str, int] = Field(default_factory=dict)
    by_portability: PortabilityCounts = Field(default_factory=PortabilityCounts)
    by_source: dict[str, int] = Field(default_factory=dict)
    by_vault: dict[str, int] = Field(default_factory=dict)
    oldest: datetime | None = None
    newest: datetime | None = None
    index_size_bytes: int = 0
    vault_paths: list[str] = Field(default_factory=list)


class FetchInput(BaseModel):
    """Input to ``fetch``."""

    model_config = ConfigDict(extra="forbid")

    id: UUID


class FetchOutput(BaseModel):
    """Output of ``fetch``: the thought, or ``None`` if not found."""

    model_config = ConfigDict(extra="ignore")

    thought: Thought | None = None


__all__ = [
    "CaptureInput",
    "CaptureInputMetadata",
    "CaptureOutput",
    "FetchInput",
    "FetchOutput",
    "Filter",
    "ListInput",
    "ListOutput",
    "PortabilityCounts",
    "SearchInput",
    "SearchOutput",
    "SortOption",
    "StatsOutput",
]
