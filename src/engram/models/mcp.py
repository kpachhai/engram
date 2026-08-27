"""MCP tool I/O Pydantic models: the wire contract for every MCP tool.

Each of the five core tools - ``capture_thought``, ``search_thoughts``,
``list_thoughts``, ``thought_stats``, ``fetch`` - has paired Input and Output
models. Inputs use ``extra="forbid"`` so unknown fields surface as clear
validation errors; outputs use ``extra="ignore"`` so the tool implementation
can pass extra context internally without polluting the wire format.

API stability: these shapes are stable for the v1.x lifetime. Only
non-breaking additions are permitted (new optional fields, new tools, new
optional filter dimensions).
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

    prefix: str | list[str] | None = Field(
        default=None,
        description=(
            "Match thoughts carrying this prefix (or any of the listed "
            "prefixes), e.g. 'Lesson' or ['Decision', 'Friction']."
        ),
    )
    portability: Portability | list[Portability] | None = Field(
        default=None,
        description=(
            "Match by privacy classification: 'portable', 'sensitive', or "
            "'block' (or a list of them)."
        ),
    )
    source: str | list[str] | None = Field(
        default=None,
        description="Match by the source attribution stamped at capture time.",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Match if ANY listed tag is present on the thought.",
    )
    vault: str | list[str] | None = Field(
        default=None,
        description=(
            "Vault name(s) to target. Under multi-vault serving, '*' opts "
            "search_thoughts into cross-vault search; a single name routes "
            "to that vault; absence means the primary vault."
        ),
    )
    created_after: datetime | None = Field(
        default=None,
        description="Only thoughts created strictly after this timestamp.",
    )
    created_before: datetime | None = Field(
        default=None,
        description="Only thoughts created strictly before this timestamp.",
    )


class CaptureInputMetadata(BaseModel):
    """Optional metadata override for ``capture_thought``.

    The ``vault`` field selects an explicit cross-vault routing target.
    Clients that omit this field land in the primary vault by default.
    """

    model_config = ConfigDict(extra="forbid")

    prefix: str | None = Field(
        default=None,
        description=(
            "Explicit prefix override. Omit to parse the prefix from a "
            "leading [Prefix] marker in content, falling back to 'Note'. "
            "Canonical prefixes: Lesson, Pattern, Decision, Friction, "
            "Resolution, Action Item, Parked, Notice, Domain, Workflow, "
            "Style, Artifact, Session Summary, Meta, Note. Non-canonical "
            "values are accepted but land outside the standard taxonomy."
        ),
    )
    portability: Portability | None = Field(
        default=None,
        description=(
            "Privacy classification stamped into the thought's frontmatter. "
            "'portable': may sync anywhere and reach any configured LLM. "
            "'sensitive': only ever reaches LOCAL LLM providers, never a "
            "remote API. 'block': never reaches any LLM and always lands in "
            "the primary vault. Omitted: defaults by prefix (Domain and "
            "Artifact default to sensitive, every other prefix to portable)."
        ),
    )
    source: str | None = Field(
        default=None,
        description=(
            "Attribution recorded in frontmatter. Omit to use the server's configured default_user."
        ),
    )
    tags: list[str] | None = Field(
        default=None,
        description="Freeform tags stored in frontmatter; filterable in search/list.",
    )
    #: Explicit target-vault alias. None means "no preference"
    #: (routing rules fire if ``auto_route: true``; otherwise lands in
    #: primary). Explicit always wins over rules.
    vault: str | None = Field(
        default=None,
        description=(
            "Explicit target-vault name. Omit for no preference: routing "
            "rules fire when auto_route is enabled, otherwise the capture "
            "lands in the primary vault. Explicit always wins over rules."
        ),
    )


class CaptureInput(BaseModel):
    """Input to ``capture_thought``."""

    model_config = ConfigDict(extra="forbid")

    content: str
    metadata: CaptureInputMetadata | None = None


class CaptureOutput(BaseModel):
    """Output of ``capture_thought``: the freshly captured thought's identity.

    ``index_state`` is additive in the v1.x stability commitment: clients that
    ignore the field continue to work, clients that read it get a real-time
    signal when the SQLite index write failed. ``"ok"`` means the row was
    inserted; ``"failed"`` means the markdown is on disk but the index row
    is absent and the thought won't appear in search/list until
    ``engram reindex`` runs. The markdown source-of-truth is always
    preserved either way.

    ``portability`` and ``source`` are additive on the same terms: they echo
    the values resolved at capture (explicit metadata, or the per-prefix /
    default_user fallbacks) so the calling agent can see a misclassified or
    mis-attributed capture immediately, before the stamp becomes permanent
    history. ``None`` only when a pre-upgrade server omits them.
    """

    model_config = ConfigDict(extra="ignore")

    id: UUID
    file_path: str
    fingerprint: str
    index_state: Literal["ok", "failed"] = "ok"
    portability: Portability | None = None
    source: str | None = None


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


class DeleteInput(BaseModel):
    """Input to ``delete_thought``.

    ``confirm`` has no default: callers MUST explicitly pass either
    ``False`` (preview) or ``True`` (commit). Forgetting the parameter
    surfaces as a validation error rather than a silent destructive
    default. AI clients are expected to call once with ``confirm=False``
    to obtain a preview, show it to the user, and only call again with
    ``confirm=True`` after explicit user approval.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(..., description="UUID of the thought to delete.")
    confirm: bool = Field(
        ...,
        description=(
            "Set False for a dry-run preview. Set True only after the user "
            "has explicitly approved the deletion shown in the preview "
            "response."
        ),
    )


class DeleteOutput(BaseModel):
    """Output of ``delete_thought``.

    ``deleted=False`` covers two cases: ``confirm=False`` dry-run preview
    (``body_preview`` populated) and ``not found`` (``body_preview`` absent,
    ``message`` reports the not-found condition).
    """

    model_config = ConfigDict(extra="ignore")

    deleted: bool
    id: UUID
    prefix: str | None = None
    portability: Portability | None = None
    created_at: datetime | None = None
    body_preview: str | None = None
    message: str


__all__ = [
    "CaptureInput",
    "CaptureInputMetadata",
    "CaptureOutput",
    "DeleteInput",
    "DeleteOutput",
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
