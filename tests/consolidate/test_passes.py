"""Tests for engram.consolidate.passes - report generation over a real tmp vault."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engram.consolidate.models import ClusterAction, PassState
from engram.consolidate.passes import (
    ReportSettings,
    generate_report,
    most_restrictive_portability,
)
from engram.errors import ConsolidateModelMismatch, LLMProviderError
from engram.storage.facade import VaultStorage

_DIM = 4
_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path: Path) -> Generator[VaultStorage, None, None]:
    vault = tmp_path / "vault"
    store = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name="test-model",
        vault_name="test-vault",
    )
    yield store
    store.close()


def _content_loader_for(storage: VaultStorage):
    def _load(thought_id: str) -> str:
        thought = storage.get_by_id(thought_id)
        assert thought is not None
        return thought.content

    return _load


def _report_for(storage: VaultStorage, **kwargs):
    defaults = {
        "conn": storage.conn,
        "vault_name": "test-vault",
        "configured_model": "test-model",
        "now": _NOW,
        "settings": ReportSettings(),
        "content_loader": _content_loader_for(storage),
        "judge": None,
        "distiller": None,
    }
    defaults.update(kwargs)
    return generate_report(**defaults)


def test_most_restrictive_portability_ordering():
    assert most_restrictive_portability(["portable", "sensitive"]) == "sensitive"
    assert most_restrictive_portability(["sensitive", "block", "portable"]) == "block"
    assert most_restrictive_portability(["portable"]) == "portable"


def test_empty_vault_produces_clean_report(storage: VaultStorage):
    report = _report_for(storage)
    assert report.clusters == []
    assert report.stale_candidates == []
    assert report.pass_near_duplicate.state is PassState.COMPLETE


def test_model_mismatch_refuses(storage: VaultStorage):
    with pytest.raises(ConsolidateModelMismatch, match="reindex"):
        _report_for(storage, configured_model="other-model")


def test_exact_duplicates_keep_newest(storage: VaultStorage):
    old = storage.capture(
        content="[Lesson] identical content",
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at=_NOW - timedelta(days=10),
    )
    new = storage.capture(
        content="[Lesson] identical content",
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at=_NOW - timedelta(days=1),
    )
    report = _report_for(storage)
    keeps = [c for c in report.clusters if c.action is ClusterAction.KEEP_NEWEST]
    assert len(keeps) == 1
    assert keeps[0].keep_thought_id == new.id
    assert keeps[0].similarity_floor == 1.0
    member_ids = {m.thought_id for m in keeps[0].members}
    assert member_ids == {old.id, new.id}


def test_near_duplicates_merge_with_distiller(storage: VaultStorage):
    first = storage.capture(
        content="[Lesson] near duplicate alpha",
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at=_NOW - timedelta(days=5),
    )
    second = storage.capture(
        content="[Lesson] near duplicate beta",
        portability="sensitive",
        embedding=[1.0, 0.05, 0.0, 0.0],
        created_at=_NOW - timedelta(days=4),
    )
    distilled_inputs: list[list[str]] = []

    def fake_distiller(members: list[tuple[str, str]], prefix: str) -> str:
        distilled_inputs.append([m[0] for m in members])
        return "distilled essence"

    report = _report_for(storage, distiller=fake_distiller)
    merges = [c for c in report.clusters if c.action is ClusterAction.MERGE]
    assert len(merges) == 1
    assert merges[0].distilled_draft == "distilled essence"
    assert merges[0].portability == "sensitive"  # most restrictive member wins
    assert {m.thought_id for m in merges[0].members} == {first.id, second.id}
    assert report.pass_merge.state is PassState.COMPLETE


def test_block_member_cluster_never_reaches_distiller(storage: VaultStorage):
    storage.capture(
        content="[Lesson] secret near dup",
        portability="block",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    storage.capture(
        content="[Lesson] public near dup",
        embedding=[1.0, 0.05, 0.0, 0.0],
    )
    calls: list[object] = []

    def spy_distiller(members: list[tuple[str, str]], prefix: str) -> str:
        calls.append(members)
        return "should never happen"

    report = _report_for(storage, distiller=spy_distiller)
    assert calls == []
    reviews = [c for c in report.clusters if c.action is ClusterAction.MANUAL_REVIEW]
    assert len(reviews) == 1
    assert "block" in (reviews[0].review_reason or "")
    assert reviews[0].portability == "block"


def test_no_llm_degrades_to_manual_review(storage: VaultStorage):
    storage.capture(content="[Lesson] one of pair", embedding=[1.0, 0.0, 0.0, 0.0])
    storage.capture(content="[Lesson] two of pair", embedding=[1.0, 0.05, 0.0, 0.0])
    report = _report_for(storage)  # judge=None, distiller=None
    assert report.pass_merge.state is PassState.SKIPPED
    assert report.pass_contradiction.state is PassState.SKIPPED
    assert all(c.action is ClusterAction.MANUAL_REVIEW for c in report.clusters)


def test_pending_embeddings_counted_and_excluded(storage: VaultStorage):
    storage.capture(content="[Lesson] pending thought")  # no embedding
    storage.capture(content="[Lesson] embedded thought", embedding=[1.0, 0.0, 0.0, 0.0])
    report = _report_for(storage)
    assert report.exclusions.pending_embeddings == 1
    assert report.clusters == []


def test_stale_and_future_dated(storage: VaultStorage):
    storage.capture(
        content="[Lesson] ancient",
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at=_NOW - timedelta(days=400),
    )
    storage.capture(
        content="[Decision] from the future",
        embedding=[0.0, 1.0, 0.0, 0.0],
        created_at=_NOW + timedelta(days=3),
    )
    report = _report_for(storage)
    assert len(report.stale_candidates) == 1
    assert report.stale_candidates[0].age_days == 400
    assert report.exclusions.future_dated == 1


def test_contradiction_judged_pairs(storage: VaultStorage):
    storage.capture(content="[Lesson] X is true", embedding=[1.0, 0.0, 0.0, 0.0])
    storage.capture(content="[Lesson] X is false", embedding=[1.0, 0.45, 0.0, 0.0])

    def fake_judge(first: str, second: str) -> tuple[str, str]:
        return "contradiction", "they disagree about X"

    settings = ReportSettings(near_dup_threshold=0.99, contradiction_threshold=0.5)
    report = _report_for(storage, settings=settings, judge=fake_judge)
    assert len(report.contradiction_candidates) == 1
    assert report.contradiction_candidates[0].rationale == "they disagree about X"
    assert report.pass_contradiction.state is PassState.COMPLETE


def test_contradiction_pass_incomplete_on_provider_failure(storage: VaultStorage):
    storage.capture(content="[Lesson] A", embedding=[1.0, 0.0, 0.0, 0.0])
    storage.capture(content="[Lesson] B", embedding=[1.0, 0.45, 0.0, 0.0])

    def failing_judge(first: str, second: str) -> tuple[str, str]:
        msg = "provider_unreachable: connection refused"
        raise LLMProviderError(msg)

    settings = ReportSettings(near_dup_threshold=0.99, contradiction_threshold=0.5)
    report = _report_for(storage, settings=settings, judge=failing_judge)
    assert report.pass_contradiction.state is PassState.INCOMPLETE
    assert "0 of 1" in (report.pass_contradiction.reason or "")


def test_prefix_filter_scopes_the_run(storage: VaultStorage):
    storage.capture(content="[Lesson] in scope", embedding=[1.0, 0.0, 0.0, 0.0])
    storage.capture(content="[Decision] out of scope", embedding=[1.0, 0.0, 0.0, 0.0])
    report = _report_for(storage, settings=ReportSettings(prefix="Lesson", stale_days=1))
    pinned = {
        str(c.thought_id) for candidate in report.stale_candidates for c in [candidate.thought]
    }
    assert len(pinned) <= 1  # only Lesson-prefix thoughts considered


def test_cross_prefix_thoughts_never_cluster(storage: VaultStorage):
    storage.capture(content="[Lesson] same vector", embedding=[1.0, 0.0, 0.0, 0.0])
    storage.capture(content="[Decision] same vector", embedding=[1.0, 0.0, 0.0, 0.0])
    report = _report_for(storage)
    near_dups = [c for c in report.clusters if c.similarity_floor < 1.0 or len(c.members) > 1]
    # identical embeddings but different prefixes: no near-dup cluster forms
    # (the fingerprints differ too, so no exact-dup cluster either)
    assert near_dups == []


def test_oversized_cluster_downgrades_to_manual_review(storage: VaultStorage):
    """Clusters above max-cluster-size are never auto-merged."""
    for index in range(3):
        storage.capture(
            content=f"[Lesson] crowded topic variant {index}",
            embedding=[1.0, 0.001 * index, 0.0, 0.0],
        )
    report = _report_for(
        storage,
        settings=ReportSettings(max_cluster_size=2),
        distiller=lambda members, prefix: "should not be called",
    )
    reviews = [c for c in report.clusters if c.action is ClusterAction.MANUAL_REVIEW]
    assert len(reviews) == 1
    assert "max-cluster-size" in (reviews[0].review_reason or "")


def test_block_thought_never_reaches_the_judge(storage: VaultStorage):
    storage.capture(
        content="[Lesson] secret claim", portability="block", embedding=[1.0, 0.0, 0.0, 0.0]
    )
    storage.capture(content="[Lesson] open claim A", embedding=[1.0, 0.45, 0.0, 0.0])
    storage.capture(content="[Lesson] open claim B", embedding=[1.0, 0.55, 0.0, 0.0])
    seen: list[str] = []

    def spy_judge(first: str, second: str) -> tuple[str, str]:
        seen.extend([first, second])
        return "consistent", "fine"

    settings = ReportSettings(near_dup_threshold=0.999, contradiction_threshold=0.5)
    report = _report_for(storage, settings=settings, judge=spy_judge)
    assert all("secret claim" not in content for content in seen)
    assert report.exclusions.block_thoughts_llm == 1


def test_default_settings_match_measured_dup_bands():
    settings = ReportSettings()
    assert settings.near_dup_threshold == 0.93
    assert settings.exclude_prefixes == ("Session Summary",)


def test_excluded_prefix_skips_similarity_passes(storage: VaultStorage):
    storage.capture(
        content="[Session Summary] wrapped the alpha work",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    storage.capture(
        content="[Session Summary] wrapped the beta work",
        embedding=[1.0, 0.05, 0.0, 0.0],
    )
    storage.capture(content="[Lesson] near duplicate alpha", embedding=[0.0, 0.0, 1.0, 0.0])
    storage.capture(content="[Lesson] near duplicate beta", embedding=[0.0, 0.05, 1.0, 0.0])
    report = _report_for(storage)
    assert {c.prefix for c in report.clusters} == {"Lesson"}
    assert report.exclusions.prefix_excluded == 2
    assert report.exclude_prefixes == ["Session Summary"]


def test_excluded_prefix_exact_duplicates_still_keep_newest(storage: VaultStorage):
    old = storage.capture(
        content="[Session Summary] identical wrap",
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at=_NOW - timedelta(days=3),
    )
    new = storage.capture(
        content="[Session Summary] identical wrap",
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at=_NOW - timedelta(days=1),
    )
    report = _report_for(storage)
    keeps = [c for c in report.clusters if c.action is ClusterAction.KEEP_NEWEST]
    assert len(keeps) == 1
    assert keeps[0].keep_thought_id == new.id
    assert {m.thought_id for m in keeps[0].members} == {old.id, new.id}
    # Exact duplicates are consumed before the similarity passes, so nothing
    # was left for the exclusion to skip.
    assert report.exclusions.prefix_excluded == 0


def test_excluded_prefix_still_reaches_staleness(storage: VaultStorage):
    storage.capture(
        content="[Session Summary] ancient wrap",
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at=_NOW - timedelta(days=400),
    )
    report = _report_for(storage)
    assert report.clusters == []
    assert len(report.stale_candidates) == 1
    assert report.exclusions.prefix_excluded == 1


def test_explicit_prefix_scope_overrides_exclusion(storage: VaultStorage):
    storage.capture(
        content="[Session Summary] wrapped the alpha work",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    storage.capture(
        content="[Session Summary] wrapped the beta work",
        embedding=[1.0, 0.05, 0.0, 0.0],
    )
    report = _report_for(storage, settings=ReportSettings(prefix="Session Summary"))
    assert {c.prefix for c in report.clusters} == {"Session Summary"}
    assert report.exclusions.prefix_excluded == 0
    assert report.exclude_prefixes == []


def test_excluded_prefix_never_reaches_the_judge(storage: VaultStorage):
    storage.capture(content="[Session Summary] claim A", embedding=[1.0, 0.0, 0.0, 0.0])
    storage.capture(content="[Session Summary] claim B", embedding=[1.0, 0.75, 0.0, 0.0])
    calls: list[tuple[str, str]] = []

    def spy_judge(first: str, second: str) -> tuple[str, str]:
        calls.append((first, second))
        return "consistent", "fine"

    report = _report_for(storage, judge=spy_judge)
    assert calls == []
    assert report.contradiction_candidates == []
