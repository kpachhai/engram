"""Journaled apply engine: execute merge proposals from a report.

Apply mutates the vault per cluster in a fixed order - journal intent,
capture the merged thought (eager embedding), archive originals out of
``thoughts_dir``, delete the superseded index rows in one transaction. A
crash leaves the vault consistent after any prefix of clusters; re-running
resumes from the journal (cluster ids are deterministic fingerprint hashes).

Every proposal is re-verified against the live vault before it runs:
missing or changed (fingerprint) or post-snapshot-modified thoughts skip
that proposal with a warning rather than applying blind.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from engram.consolidate.models import (
    ApplyResult,
    ClusterAction,
    ClusterProposal,
    ConsolidationReport,
    JournalEntry,
    JournalEntryState,
)
from engram.errors import VaultError
from engram.storage.archive import archive_thought_file
from engram.storage.sqlite_queries import delete_thought_rows, get_thought_row
from engram.sync.gitops import commit_paths

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from engram.storage.facade import VaultStorage

_log = logging.getLogger("engram.consolidate.apply")

#: Embeds the merged thought's content; None = land as pending + doctor repair.
EmbedFn = Callable[[str], Sequence[float]]


def _append_journal(journal_path: Path, entry: JournalEntry) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json() + "\n")
        handle.flush()


def load_journal_state(journal_dir: Path) -> dict[str, JournalEntry]:
    """Last journal entry per cluster across ALL journals in the dir.

    Malformed lines are tolerated (a crash can truncate the final line).
    """
    state: dict[str, JournalEntry] = {}
    if not journal_dir.exists():
        return state
    for journal_file in sorted(journal_dir.glob("journal-*.jsonl")):
        for line in journal_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = JournalEntry.model_validate_json(line)
            except ValueError:
                _log.warning("skipping malformed journal line in %s", journal_file)
                continue
            state[entry.cluster_id] = entry
    return state


def _verify_members(
    proposal: ClusterProposal,
    *,
    conn: sqlite3.Connection,
    snapshot_at: datetime,
) -> tuple[bool, str | None, int]:
    """Check every member still matches its report pin.

    Returns ``(ok, skip_reason, missing_count)``. Missing rows are not an
    automatic skip - a resumed run may already have archived some members.
    Changed (fingerprint) or post-snapshot-modified members always skip.
    """
    from engram.consolidate.passes import _row_dt  # shared datetime coercion

    missing = 0
    for member in proposal.members:
        row = get_thought_row(conn, member.thought_id)
        if row is None:
            missing += 1
            continue
        if str(row["fingerprint"]) != member.fingerprint:
            return False, f"thought {member.thought_id} changed since the report", missing
        if _row_dt(row, "updated_at") >= snapshot_at:
            return (
                False,
                f"thought {member.thought_id} was modified after the report snapshot",
                missing,
            )
    return True, None, missing


def apply_report(
    *,
    storage: VaultStorage,
    report: ConsolidationReport,
    report_path: Path,
    archive_dir: Path,
    journal_dir: Path,
    embed_fn: EmbedFn | None,
    now: datetime,
    commit: bool = True,
) -> ApplyResult:
    """Execute the report's actionable proposals (merge + keep-newest).

    Stale, contradiction, and manual-review findings are never acted on.
    The caller (CLI Layer) is responsible for holding the vault lock.
    """
    journal_state = load_journal_state(journal_dir)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    journal_path = journal_dir / f"journal-{stamp}.jsonl"

    applied = 0
    skipped = 0
    failed = 0
    id_map: dict[str, str] = {}
    touched_paths: list[Path] = []

    for proposal in report.clusters:
        if proposal.action is ClusterAction.MANUAL_REVIEW:
            continue
        prior = journal_state.get(proposal.cluster_id)
        if prior is not None and prior.state is JournalEntryState.COMPLETED:
            skipped += 1
            _log.info("cluster %s already applied; skipping", proposal.cluster_id)
            continue

        ok, reason, _missing = _verify_members(
            proposal, conn=storage.conn, snapshot_at=report.snapshot_at
        )
        if not ok:
            skipped += 1
            _append_journal(
                journal_path,
                JournalEntry(
                    cluster_id=proposal.cluster_id,
                    state=JournalEntryState.SKIPPED,
                    at=now,
                    detail=reason,
                ),
            )
            _log.warning("skipping cluster %s: %s", proposal.cluster_id, reason)
            continue

        try:
            outcome = _apply_cluster(
                proposal,
                storage=storage,
                archive_dir=archive_dir,
                journal_path=journal_path,
                prior=prior,
                embed_fn=embed_fn,
                now=now,
            )
        except (VaultError, sqlite3.Error, OSError) as exc:
            failed += 1
            _append_journal(
                journal_path,
                JournalEntry(
                    cluster_id=proposal.cluster_id,
                    state=JournalEntryState.FAILED,
                    at=now,
                    detail=str(exc),
                ),
            )
            _log.exception("cluster %s failed; continuing with the rest", proposal.cluster_id)
            continue
        applied += 1
        id_map.update(outcome[0])
        touched_paths.extend(outcome[1])

    commit_sha: str | None = None
    if commit and touched_paths:
        commit_sha = _commit_touched(storage, touched_paths, applied=applied)

    return ApplyResult(
        report_path=str(report_path),
        applied=applied,
        skipped=skipped,
        failed=failed,
        id_map=id_map,
        commit=commit_sha,
    )


def _apply_cluster(
    proposal: ClusterProposal,
    *,
    storage: VaultStorage,
    archive_dir: Path,
    journal_path: Path,
    prior: JournalEntry | None,
    embed_fn: EmbedFn | None,
    now: datetime,
) -> tuple[dict[str, str], list[Path]]:
    """Apply one cluster; returns (id_map fragment, touched paths)."""
    _append_journal(
        journal_path,
        JournalEntry(cluster_id=proposal.cluster_id, state=JournalEntryState.INTENT, at=now),
    )

    touched: list[Path] = []
    if proposal.action is ClusterAction.MERGE:
        keep_id = _resume_or_capture_merged(
            proposal,
            storage=storage,
            journal_path=journal_path,
            prior=prior,
            embed_fn=embed_fn,
            now=now,
        )
        merged_row = get_thought_row(storage.conn, keep_id)
        if merged_row is not None:
            touched.append((storage.thoughts_dir / str(merged_row["file_path"])).resolve())
        to_archive = list(proposal.members)
    else:  # KEEP_NEWEST
        if proposal.keep_thought_id is None:  # pragma: no cover - model validator forbids
            msg = "keep-newest proposal without keep_thought_id"
            raise VaultError(msg)
        keep_id = str(proposal.keep_thought_id)
        to_archive = [m for m in proposal.members if str(m.thought_id) != keep_id]

    archived_paths: list[str] = []
    archive_ids: list[str] = []
    for member in to_archive:
        row = get_thought_row(storage.conn, member.thought_id)
        if row is None:
            # Already archived by an interrupted run; archive helper resume
            # below still verifies the file landed.
            continue
        _, destination = archive_thought_file(
            thoughts_dir=storage.thoughts_dir,
            archive_dir=archive_dir,
            rel_path=str(row["file_path"]),
            superseded_by=UUID(keep_id),
            archived_at=now,
        )
        archived_paths.append(str(destination))
        archive_ids.append(str(member.thought_id))
        touched.append(destination)
        touched.append((storage.thoughts_dir / str(row["file_path"])).resolve())

    _append_journal(
        journal_path,
        JournalEntry(
            cluster_id=proposal.cluster_id,
            state=JournalEntryState.ORIGINALS_ARCHIVED,
            at=now,
            merged_thought_id=UUID(keep_id),
            archived_paths=archived_paths,
        ),
    )

    if archive_ids:
        delete_thought_rows(storage.conn, archive_ids)

    _append_journal(
        journal_path,
        JournalEntry(
            cluster_id=proposal.cluster_id,
            state=JournalEntryState.COMPLETED,
            at=now,
            merged_thought_id=UUID(keep_id),
            archived_paths=archived_paths,
        ),
    )
    id_map = {archived: keep_id for archived in archive_ids}
    return id_map, touched


def _resume_or_capture_merged(
    proposal: ClusterProposal,
    *,
    storage: VaultStorage,
    journal_path: Path,
    prior: JournalEntry | None,
    embed_fn: EmbedFn | None,
    now: datetime,
) -> str:
    """Capture the distilled thought, or reuse the one a prior run captured."""
    if (
        prior is not None
        and prior.merged_thought_id is not None
        and prior.state in (JournalEntryState.MERGED_CAPTURED, JournalEntryState.ORIGINALS_ARCHIVED)
        and get_thought_row(storage.conn, prior.merged_thought_id) is not None
    ):
        return str(prior.merged_thought_id)

    if proposal.distilled_draft is None:  # pragma: no cover - model validator forbids
        msg = "merge proposal without distilled_draft"
        raise VaultError(msg)

    embedding: Sequence[float] | None = None
    embed_note: str | None = None
    if embed_fn is not None:
        try:
            embedding = embed_fn(proposal.distilled_draft)
        except Exception as exc:
            embed_note = f"embedding failed; merged thought lands pending: {exc}"
            _log.warning("%s", embed_note)

    index_errors: list[sqlite3.Error] = []

    def _on_index_failure(_thought: object, exc: sqlite3.Error) -> None:
        index_errors.append(exc)

    merged = storage.capture(
        content=proposal.distilled_draft,
        prefix=proposal.prefix,
        portability=proposal.portability,
        source="engram-consolidate",
        embedding=embedding,
        extra_frontmatter=_provenance_for(proposal, conn=storage.conn),
        on_index_failure=_on_index_failure,
    )
    if index_errors:
        # Without an index row the merged knowledge would be invisible while
        # its sources get archived - fail the cluster and undo the markdown.
        merged.file_path.unlink(missing_ok=True)
        msg = f"index insert failed for merged thought: {index_errors[0]}"
        raise VaultError(msg)

    _append_journal(
        journal_path,
        JournalEntry(
            cluster_id=proposal.cluster_id,
            state=JournalEntryState.MERGED_CAPTURED,
            at=now,
            merged_thought_id=merged.id,
            detail=embed_note,
        ),
    )
    return str(merged.id)


def _provenance_for(proposal: ClusterProposal, *, conn: sqlite3.Connection) -> dict[str, object]:
    """Provenance frontmatter for a merged thought: source ids + date range."""
    from engram.consolidate.passes import _row_dt

    extra: dict[str, object] = {
        "consolidated_from": [str(m.thought_id) for m in proposal.members],
    }
    created_values = []
    for member in proposal.members:
        row = get_thought_row(conn, member.thought_id)
        if row is not None:
            created_values.append(_row_dt(row, "created_at"))
    if created_values:
        extra["consolidated_range"] = [
            min(created_values).isoformat(),
            max(created_values).isoformat(),
        ]
    return extra


def _commit_touched(storage: VaultStorage, touched: list[Path], *, applied: int) -> str | None:
    """One git commit for everything apply touched; non-git vaults skip."""
    vault_root = storage.thoughts_dir.parent
    if not (vault_root / ".git").exists():
        _log.info(
            "vault at %s is not a git repository; archive applied on disk, no commit",
            vault_root,
        )
        return None
    result = asyncio.run(
        commit_paths(
            vault_root,
            touched,
            message=f"engram consolidate: apply {applied} proposal(s)",
        )
    )
    return result.sha


__all__ = ["EmbedFn", "apply_report", "load_journal_state"]
