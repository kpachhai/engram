"""Tests for engram.consolidate.models - report and journal boundary models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from engram.consolidate.models import (
    ApplyResult,
    ClusterAction,
    ClusterProposal,
    ConsolidationReport,
    ContradictionCandidate,
    ContradictionVerdict,
    ExclusionCounts,
    JournalEntry,
    JournalEntryState,
    PassState,
    PassStatus,
    PinnedThought,
    StaleAnchor,
    StaleCandidate,
)

_FP = "a" * 64
_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _pinned() -> PinnedThought:
    return PinnedThought(thought_id=uuid4(), fingerprint=_FP)


def _cluster(
    action: ClusterAction = ClusterAction.MANUAL_REVIEW, **overrides: object
) -> dict[str, Any]:
    members = [_pinned(), _pinned()]
    base: dict[str, Any] = {
        "cluster_id": "c-0",
        "action": action,
        "prefix": "Lesson",
        "members": members,
        "similarity_floor": 0.92,
        "portability": "portable",
    }
    if action is ClusterAction.MANUAL_REVIEW:
        base["review_reason"] = "cluster contains a block-portability member"
    if action is ClusterAction.KEEP_NEWEST:
        base["keep_thought_id"] = members[0].thought_id
    if action is ClusterAction.MERGE:
        base["distilled_draft"] = "distilled content"
    base.update(overrides)
    return base


def _report(**overrides: object) -> dict[str, Any]:
    complete = PassStatus(state=PassState.COMPLETE, done=4, total=4)
    base: dict[str, Any] = {
        "vault_name": "personal",
        "generated_at": _NOW,
        "snapshot_at": _NOW,
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "near_dup_threshold": 0.9,
        "contradiction_threshold": 0.75,
        "stale_days": 180,
        "max_cluster_size": 12,
        "pass_near_duplicate": complete,
        "pass_stale": complete,
        "pass_contradiction": complete,
        "pass_merge": complete,
        "exclusions": ExclusionCounts(),
        "clusters": [ClusterProposal(**_cluster())],
        "stale_candidates": [],
        "contradiction_candidates": [],
    }
    base.update(overrides)
    return base


class TestPassStatus:
    def test_complete_needs_no_reason(self):
        status = PassStatus(state=PassState.COMPLETE)
        assert status.reason is None

    @pytest.mark.parametrize("state", [PassState.INCOMPLETE, PassState.SKIPPED])
    def test_incomplete_and_skipped_require_reason(self, state: PassState):
        with pytest.raises(ValidationError, match="reason"):
            PassStatus(state=state)

    def test_incomplete_carries_progress(self):
        status = PassStatus(
            state=PassState.INCOMPLETE, reason="daily cost cap exceeded", done=40, total=200
        )
        assert (status.done, status.total) == (40, 200)

    def test_negative_progress_rejected(self):
        with pytest.raises(ValidationError):
            PassStatus(state=PassState.COMPLETE, done=-1)


class TestPinnedThought:
    def test_fingerprint_must_be_64_hex(self):
        with pytest.raises(ValidationError, match="fingerprint"):
            PinnedThought(thought_id=uuid4(), fingerprint="not-hex")

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            PinnedThought.model_validate(
                {"thought_id": str(uuid4()), "fingerprint": _FP, "extra_field": "x"}
            )


class TestClusterProposal:
    def test_merge_requires_distilled_draft(self):
        data = _cluster(ClusterAction.MERGE)
        del data["distilled_draft"]
        with pytest.raises(ValidationError, match="distilled_draft"):
            ClusterProposal(**data)

    def test_keep_newest_requires_member_keep_id(self):
        data = _cluster(ClusterAction.KEEP_NEWEST)
        del data["keep_thought_id"]
        with pytest.raises(ValidationError, match="keep_thought_id"):
            ClusterProposal(**data)

    def test_keep_id_must_be_a_member(self):
        data = _cluster(ClusterAction.KEEP_NEWEST, keep_thought_id=uuid4())
        with pytest.raises(ValidationError, match="member"):
            ClusterProposal(**data)

    def test_manual_review_requires_reason(self):
        data = _cluster(ClusterAction.MANUAL_REVIEW)
        del data["review_reason"]
        with pytest.raises(ValidationError, match="review_reason"):
            ClusterProposal(**data)

    def test_single_member_cluster_rejected(self):
        data = _cluster(members=[_pinned()])
        with pytest.raises(ValidationError):
            ClusterProposal(**data)

    def test_similarity_floor_bounded(self):
        with pytest.raises(ValidationError):
            ClusterProposal(**_cluster(similarity_floor=1.5))

    def test_identical_content_floor_of_one_accepted(self):
        proposal = ClusterProposal(**_cluster(similarity_floor=1.0))
        assert proposal.similarity_floor == 1.0


class TestStaleAndContradiction:
    def test_stale_candidate_roundtrip(self):
        candidate = StaleCandidate(thought=_pinned(), age_days=365, anchor=StaleAnchor.LEGACY)
        again = StaleCandidate.model_validate_json(candidate.model_dump_json())
        assert again == candidate

    def test_negative_age_rejected(self):
        with pytest.raises(ValidationError):
            StaleCandidate(thought=_pinned(), age_days=-1, anchor=StaleAnchor.CREATED)

    def test_contradiction_candidate_requires_rationale(self):
        with pytest.raises(ValidationError):
            ContradictionCandidate(
                first=_pinned(),
                second=_pinned(),
                similarity=0.8,
                verdict=ContradictionVerdict.CONTRADICTION,
                rationale="",
            )


class TestConsolidationReport:
    def test_roundtrip_json(self):
        report = ConsolidationReport(**_report())
        again = ConsolidationReport.model_validate_json(report.model_dump_json())
        assert again == report

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            ConsolidationReport.model_validate({**_report(), "surprise": "x"})

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            ConsolidationReport(**_report(generated_at=datetime(2026, 6, 9)))

    def test_schema_version_defaults_to_one(self):
        assert ConsolidationReport(**_report()).schema_version == 1


class TestJournalEntry:
    def test_jsonl_roundtrip(self):
        entry = JournalEntry(
            cluster_id="c-0",
            state=JournalEntryState.MERGED_CAPTURED,
            at=_NOW,
            merged_thought_id=uuid4(),
        )
        line = entry.model_dump_json()
        assert "\n" not in line
        assert JournalEntry.model_validate_json(line) == entry

    def test_failed_state_carries_detail(self):
        entry = JournalEntry(
            cluster_id="c-1",
            state=JournalEntryState.FAILED,
            at=_NOW,
            detail="sqlite insert failed",
        )
        assert entry.detail is not None


class TestApplyResult:
    def test_defaults_and_id_map(self):
        old_id, merged_id = str(uuid4()), str(uuid4())
        result = ApplyResult(
            report_path="r.json", applied=1, skipped=0, failed=0, id_map={old_id: merged_id}
        )
        assert result.commit is None
        assert result.id_map[old_id] == merged_id

    def test_negative_counts_rejected(self):
        with pytest.raises(ValidationError):
            ApplyResult(report_path="r.json", applied=-1, skipped=0, failed=0)
