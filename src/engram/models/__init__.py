"""Pydantic models for engram boundary types.

Three module groups:

* :mod:`engram.models.frontmatter` - YAML frontmatter schema, the canonical
  prefix vocabulary, and the portability literal type.
* :mod:`engram.models.thought` - the runtime Thought object exposed via MCP
  and used by the storage layer.
* :mod:`engram.models.mcp` - Pydantic models for the 5 MCP tools' input and
  output shapes per ``02-TECHNICAL_DESIGN.md`` MCP API Contract.
"""

from __future__ import annotations

from engram.models.frontmatter import (
    CANONICAL_PREFIXES,
    DEFAULT_PORTABILITY_BY_PREFIX,
    Frontmatter,
    Portability,
    is_canonical_prefix,
)
from engram.models.mcp import (
    CaptureInput,
    CaptureInputMetadata,
    CaptureOutput,
    FetchInput,
    FetchOutput,
    Filter,
    ListInput,
    ListOutput,
    PortabilityCounts,
    SearchInput,
    SearchOutput,
    SortOption,
    StatsOutput,
)
from engram.models.thought import Thought, ThoughtWithSimilarity

__all__ = [
    "CANONICAL_PREFIXES",
    "DEFAULT_PORTABILITY_BY_PREFIX",
    "CaptureInput",
    "CaptureInputMetadata",
    "CaptureOutput",
    "FetchInput",
    "FetchOutput",
    "Filter",
    "Frontmatter",
    "ListInput",
    "ListOutput",
    "Portability",
    "PortabilityCounts",
    "SearchInput",
    "SearchOutput",
    "SortOption",
    "StatsOutput",
    "Thought",
    "ThoughtWithSimilarity",
    "is_canonical_prefix",
]
