"""Boundary models for consolidation reports, journals, and apply results.

The report is written to ``<vault>/.indexes/consolidate/report-<utc-ts>.json``
(per-machine state, never synced) and read back by ``--apply``. Every proposal
pins its targets to ``(thought_id, fingerprint)`` so apply can detect and skip
thoughts that changed between report and apply time.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engram.models.frontmatter import Portability

_FINGERPRINT_HEX_RE = re.compile(r"^[a-f0-9]{64}$")


class PassState(StrEnum):
    """Completion state of one detection pass."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"


class PassStatus(BaseModel):
    """Honest completion status for a detection pass.

    A pass interrupted by provider failure or a budget cap reports
    ``incomplete`` with how far it got; partial results are never presented
    as a clean pass.
    """

    model_config = ConfigDict(extra="forbid")

    state: PassState
    reason: str | None = None
    done: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _non_complete_requires_reason(self) -> Self:
        if self.state is not PassState.COMPLETE and not self.reason:
            msg = f"PassStatus state={self.state.value!r} requires a reason"
            raise ValueError(msg)
        return self


class PinnedThought(BaseModel):
    """A thought reference pinned to its content and portability at report time.

    The fingerprint covers the body only, so a portability re-tag between
    report and apply leaves it unchanged; portability is pinned separately
    because apply writes the merged thought at the report-time tier.
    """

    model_config = ConfigDict(extra="forbid")

    thought_id: UUID
    fingerprint: str
    portability: Portability

    @field_validator("fingerprint")
    @classmethod
    def _check_fingerprint(cls, value: str) -> str:
        if not _FINGERPRINT_HEX_RE.fullmatch(value):
            msg = f"fingerprint must be 64 lowercase hex characters: {value!r}"
            raise ValueError(msg)
        return value


class ClusterAction(StrEnum):
    """What apply would do with a cluster proposal."""

    #: Distill members into one merged thought; archive all originals.
    MERGE = "merge"
    #: Identical-content cluster: keep one member, archive the rest (no LLM).
    KEEP_NEWEST = "keep-newest"
    #: Surfaced for the operator; apply never acts on these.
    MANUAL_REVIEW = "manual-review"


class ClusterProposal(BaseModel):
    """One near-duplicate cluster and the proposed consolidation action."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str = Field(min_length=1)
    action: ClusterAction
    prefix: str = Field(min_length=1)
    members: list[PinnedThought] = Field(min_length=2)
    #: Lowest pairwise similarity that admitted a member; 1.0 for
    #: identical-content clusters.
    similarity_floor: float = Field(ge=0.0, le=1.0)
    #: Most restrictive portability among members (block > sensitive > portable).
    portability: Portability
    keep_thought_id: UUID | None = None
    distilled_draft: str | None = None
    review_reason: str | None = None

    @model_validator(mode="after")
    def _action_invariants(self) -> Self:
        if self.action is ClusterAction.MERGE and not self.distilled_draft:
            msg = "action=merge requires distilled_draft"
            raise ValueError(msg)
        if self.action is ClusterAction.KEEP_NEWEST:
            if self.keep_thought_id is None:
                msg = "action=keep-newest requires keep_thought_id"
                raise ValueError(msg)
            if self.keep_thought_id not in {m.thought_id for m in self.members}:
                msg = "keep_thought_id must be a cluster member"
                raise ValueError(msg)
        if self.action is ClusterAction.MANUAL_REVIEW and not self.review_reason:
            msg = "action=manual-review requires review_reason"
            raise ValueError(msg)
        return self


class StaleAnchor(StrEnum):
    """Which timestamp anchored a stale candidate's age."""

    CREATED = "created_at"
    UPDATED = "updated_at"
    LEGACY = "legacy_created_at"


class StaleCandidate(BaseModel):
    """Report-only: a thought whose anchor timestamp exceeds the age threshold."""

    model_config = ConfigDict(extra="forbid")

    thought: PinnedThought
    age_days: int = Field(ge=0)
    anchor: StaleAnchor


class ContradictionVerdict(StrEnum):
    """LLM verdict on a high-similarity pair; consistent pairs are not reported."""

    CONTRADICTION = "contradiction"
    UNCLEAR = "unclear"


class ContradictionCandidate(BaseModel):
    """Report-only: a pair the LLM judged contradictory (or could not resolve)."""

    model_config = ConfigDict(extra="forbid")

    first: PinnedThought
    second: PinnedThought
    similarity: float = Field(ge=0.0, le=1.0)
    verdict: ContradictionVerdict
    rationale: str = Field(min_length=1)


class ExclusionCounts(BaseModel):
    """Per-run accounting of thoughts each pass could not consider."""

    model_config = ConfigDict(extra="forbid")

    pending_embeddings: int = Field(default=0, ge=0)
    failed_embeddings: int = Field(default=0, ge=0)
    #: Thoughts excluded from LLM passes by the portability gate.
    block_thoughts_llm: int = Field(default=0, ge=0)
    future_dated: int = Field(default=0, ge=0)
    #: Thoughts or pairs exceeding the provider context window.
    oversized: int = Field(default=0, ge=0)
    #: Thoughts skipped by the prefix-exclusion list (similarity passes only).
    prefix_excluded: int = Field(default=0, ge=0)


class ConsolidationReport(BaseModel):
    """The full output of a consolidation report run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    vault_name: str = Field(min_length=1)
    generated_at: datetime
    #: Thoughts created or updated at/after this instant were not considered.
    snapshot_at: datetime
    embedding_model: str = Field(min_length=1)
    near_dup_threshold: float = Field(ge=0.0, le=1.0)
    contradiction_threshold: float = Field(ge=0.0, le=1.0)
    stale_days: int = Field(ge=1)
    max_cluster_size: int = Field(ge=2)
    #: Prefixes the similarity passes skipped this run (empty when --prefix
    #: scoped the run explicitly).
    exclude_prefixes: list[str] = Field(default_factory=list)
    pass_near_duplicate: PassStatus
    pass_stale: PassStatus
    pass_contradiction: PassStatus
    pass_merge: PassStatus
    exclusions: ExclusionCounts
    clusters: list[ClusterProposal]
    stale_candidates: list[StaleCandidate]
    contradiction_candidates: list[ContradictionCandidate]

    @field_validator("generated_at", "snapshot_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "datetime must be timezone-aware (UTC)"
            raise ValueError(msg)
        return value


class JournalEntryState(StrEnum):
    """Per-cluster apply progress; one JSONL line per transition."""

    INTENT = "intent"
    MERGED_CAPTURED = "merged-captured"
    ORIGINALS_ARCHIVED = "originals-archived"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class JournalEntry(BaseModel):
    """One line of the apply journal (``journal-<utc-ts>.jsonl``)."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str = Field(min_length=1)
    state: JournalEntryState
    at: datetime
    merged_thought_id: UUID | None = None
    archived_paths: list[str] = Field(default_factory=list)
    detail: str | None = None

    @field_validator("at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "datetime must be timezone-aware (UTC)"
            raise ValueError(msg)
        return value


class ApplyResult(BaseModel):
    """Outcome summary of one ``--apply`` run."""

    model_config = ConfigDict(extra="forbid")

    report_path: str = Field(min_length=1)
    applied: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    #: Archived thought id -> superseding merged thought id.
    id_map: dict[str, str] = Field(default_factory=dict)
    #: Consolidation git commit sha; None for non-git vaults or no-op runs.
    commit: str | None = None


__all__ = [
    "ApplyResult",
    "ClusterAction",
    "ClusterProposal",
    "ConsolidationReport",
    "ContradictionCandidate",
    "ContradictionVerdict",
    "ExclusionCounts",
    "JournalEntry",
    "JournalEntryState",
    "PassState",
    "PassStatus",
    "PinnedThought",
    "StaleAnchor",
    "StaleCandidate",
]
