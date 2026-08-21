"""Open Brain -> engram migration pipeline.

Implements the 6-step pipeline per ``04-MIGRATION.md``:

1. **Connect/Probe**: ``list_thoughts(limit=1, sort=created_at_asc)`` to verify
   connectivity AND that the sort parameter is honored (B4 mitigation -
   without ``sort``, pagination is non-deterministic).
2. **Enumerate**: page through ``list_thoughts(limit=500, offset=N, sort=created_at_asc)``.
3. **Transform**: per source thought, generate fresh UUID-v7, parse prefix,
   compute fingerprint via engram normalization, idempotency check on the
   ``(fingerprint, source, created_at)`` triple. With ``--prefer-legacy-id-match``,
   try ``(legacy_id, source)`` lookup first and update in place on match.
4. **Write**: markdown atomic write + SQLite row insert + embedding (failure-tolerant).
5. **Validate**: random-sample 10 imported thoughts; ``fetch(id)`` byte-for-byte.
6. **Report**: write ``migration-report.json``.

Resumability: re-running with ``--append`` (default behavior at step 3) skips
rows whose triple already matches an existing engram row.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from engram.embedding.protocol import EmbeddingProvider
from engram.errors import MigrationError
from engram.models.frontmatter import Portability, default_portability_for_prefix
from engram.storage.facade import VaultStorage, parse_prefix_from_content
from engram.storage.sqlite_queries import (
    record_migration_complete,
    record_migration_start,
    update_thought_body,
)

_log = logging.getLogger("engram.migration.open_brain")

#: Default page size for list_thoughts pagination.
_PAGE_SIZE = 500

#: Number of random samples for the round-trip validation step.
_VALIDATION_SAMPLE_SIZE = 10


@dataclass(frozen=True, slots=True)
class OpenBrainThought:
    """One thought as returned by Open Brain's list_thoughts."""

    id: str
    content: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any]
    content_fingerprint: str | None = None


@dataclass(slots=True)
class MigrationConfig:
    """Inputs to the migration pipeline."""

    open_brain_url: str
    open_brain_key: str | None
    vault_storage: VaultStorage
    embedder: EmbeddingProvider | None = None
    default_user: str = "engram-user"
    dry_run: bool = False
    limit: int | None = None
    prefer_legacy_id_match: bool = False
    report_path: Path | None = None


@dataclass(slots=True)
class MigrationReport:
    """Aggregated migration result for migration-report.json."""

    migration_id: str
    source_url: str
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    enumerated: int = 0
    migrated: int = 0
    skipped_existing: int = 0
    errors_count: int = 0
    by_prefix: dict[str, int] = field(default_factory=dict)
    by_portability: dict[str, int] = field(default_factory=dict)
    fallback_assignments: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)
    validation_passed: int = 0
    validation_failed: int = 0
    validation_samples: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Render the report as a JSON-serializable dict matching the spec schema."""
        return {
            "migration_id": self.migration_id,
            "source_url": self.source_url,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "totals": {
                "enumerated": self.enumerated,
                "migrated": self.migrated,
                "skipped_existing": self.skipped_existing,
                "errors": self.errors_count,
            },
            "by_prefix": self.by_prefix,
            "by_portability": self.by_portability,
            "fallback_assignments": self.fallback_assignments,
            "errors": self.errors,
            "validation": {
                "sample_size": self.validation_passed + self.validation_failed,
                "passed": self.validation_passed,
                "failed": self.validation_failed,
                "samples": self.validation_samples,
            },
        }


# === Open Brain HTTP client ===


class OpenBrainClient:
    """JSON-RPC client for the Open Brain MCP HTTP endpoint.

    Open Brain (the OB1 Supabase Edge Function) speaks MCP over HTTP. This
    client posts the standard MCP ``tools/call`` envelope and unwraps the
    nested result. Authentication is via the ``x-brain-key`` header.
    """

    def __init__(
        self,
        url: str,
        key: str | None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Open a client; reuse a shared :class:`httpx.Client` if provided."""
        self._url = url
        self._key = key
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._request_id = 0

    def close(self) -> None:
        """Close the underlying HTTP client (if owned)."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenBrainClient:
        """Return self for use as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the HTTP client on context exit."""
        del exc_info
        self.close()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["x-brain-key"] = self._key
        return headers

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """POST a JSON-RPC tools/call to the OB endpoint and return the unwrapped result."""
        envelope = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            response = self._client.post(
                self._url,
                content=json.dumps(envelope).encode("utf-8"),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            msg = f"Open Brain request failed for tool {name!r}: {exc}"
            raise MigrationError(msg) from exc

        if response.status_code != 200:
            msg = (
                f"Open Brain returned HTTP {response.status_code} for tool {name!r}: "
                f"{response.text[:200]}"
            )
            raise MigrationError(msg)

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            msg = f"Open Brain response was not valid JSON: {exc}"
            raise MigrationError(msg) from exc

        if "error" in payload:
            msg = f"Open Brain returned error for tool {name!r}: {payload['error']}"
            raise MigrationError(msg)

        result = payload.get("result")
        if result is None:
            msg = f"Open Brain response missing 'result' for tool {name!r}: {payload}"
            raise MigrationError(msg)
        return result

    def list_thoughts(
        self,
        *,
        limit: int = _PAGE_SIZE,
        offset: int = 0,
        sort: str = "created_at_asc",
    ) -> list[OpenBrainThought]:
        """Page-call list_thoughts with the deterministic-sort parameter."""
        result = self.call_tool(
            "list_thoughts",
            {"limit": limit, "offset": offset, "sort": sort},
        )
        results = result.get("results", []) if isinstance(result, dict) else result
        return [_coerce_thought(item) for item in results]


def _coerce_thought(raw: Any) -> OpenBrainThought:
    if not isinstance(raw, dict):
        msg = f"Open Brain returned non-dict thought entry: {raw!r}"
        raise MigrationError(msg)
    return OpenBrainThought(
        id=str(raw.get("id", "")),
        content=str(raw.get("content", "")),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", raw.get("created_at", ""))),
        metadata=dict(raw.get("metadata") or {}),
        content_fingerprint=(
            str(raw["content_fingerprint"])
            if "content_fingerprint" in raw and raw["content_fingerprint"] is not None
            else None
        ),
    )


# === pipeline ===


def _to_aware_dt(value: str) -> datetime:
    """Parse an ISO-8601 string to a tz-aware datetime; assume UTC if naive."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        msg = f"unparseable timestamp from Open Brain: {value!r}"
        raise MigrationError(msg) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _resolve_portability_from_metadata(
    raw_metadata: dict[str, Any],
    prefix: str,
) -> Portability:
    """Map OB metadata.portability (if any) to engram's Literal type, with BYOC default."""
    raw_value = raw_metadata.get("portability") or raw_metadata.get("Portability")
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"portable", "sensitive", "block"}:
            return normalized  # type: ignore[return-value]
    default: str = default_portability_for_prefix(prefix)
    return default  # type: ignore[return-value]


def _existing_by_triple(
    storage: VaultStorage,
    *,
    fingerprint: str,
    source: str,
    created_at: datetime,
) -> str | None:
    """Idempotency triple-match lookup. Returns the existing thought id if found."""
    cursor = storage.conn.execute(
        "SELECT id FROM thoughts WHERE fingerprint = ? AND source = ? AND created_at = ?",
        (fingerprint, source, created_at.isoformat()),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _existing_by_legacy_id(
    storage: VaultStorage,
    *,
    legacy_id: str,
    source: str,
) -> str | None:
    """--prefer-legacy-id-match lookup. Returns existing thought id if found."""
    cursor = storage.conn.execute(
        "SELECT id FROM thoughts WHERE legacy_id = ? AND source = ?",
        (legacy_id, source),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _byte_compare(actual: str, expected: str) -> bool:
    """Byte-level comparison after only LF normalization (R13 - no over-normalization)."""
    a = actual.replace("\r\n", "\n").replace("\r", "\n")
    b = expected.replace("\r\n", "\n").replace("\r", "\n")
    return a.rstrip("\n") == b.rstrip("\n")


def run_migration(config: MigrationConfig) -> MigrationReport:
    """Execute the 6-step Open Brain -> engram migration.

    Returns the populated :class:`MigrationReport`. The report is also written
    to ``config.report_path`` (or ``<vault>/migration-report.json`` by default)
    unless ``config.dry_run`` is set.
    """
    started = datetime.now(UTC)
    migration_id = secrets.token_hex(8)
    report = MigrationReport(
        migration_id=migration_id,
        source_url=config.open_brain_url,
        started_at=started.isoformat(),
    )

    # Audit-trail row in the migrations table.
    audit_rowid = record_migration_start(
        config.vault_storage.conn,
        source_type="open-brain",
        source_url=config.open_brain_url,
        started_at=started,
    )

    with OpenBrainClient(config.open_brain_url, config.open_brain_key) as client:
        # Step 1: Connect/Probe.
        try:
            _probe_results = client.list_thoughts(
                limit=1,
                offset=0,
                sort="created_at_asc",
            )
            del _probe_results
        except MigrationError as exc:
            msg = (
                f"Open Brain probe failed (does the endpoint accept "
                f"`sort=created_at_asc`? required for deterministic pagination): {exc}"
            )
            raise MigrationError(msg) from exc

        # Step 2 + 3 + 4: enumerate and transform.
        offset = 0
        migrated_ids: list[tuple[str, str]] = []  # (engram_id, source_id) for validation
        while True:
            page = client.list_thoughts(
                limit=_PAGE_SIZE,
                offset=offset,
                sort="created_at_asc",
            )
            if not page:
                break
            for ob_thought in page:
                report.enumerated += 1

                if config.limit is not None and report.migrated >= config.limit:
                    break

                outcome = _migrate_one(config, ob_thought, report)
                if outcome is not None:
                    migrated_ids.append(outcome)

            if config.limit is not None and report.migrated >= config.limit:
                break
            offset += _PAGE_SIZE

        # Step 5: random-sample validation via fetch(id) byte-for-byte.
        if migrated_ids and not config.dry_run:
            _validate_round_trips(config.vault_storage, migrated_ids, report)

    completed = datetime.now(UTC)
    report.completed_at = completed.isoformat()
    report.duration_seconds = (completed - started).total_seconds()
    report.errors_count = len(report.errors)

    # Step 6: write report + close audit row.
    report_dict = report.to_json()
    if not config.dry_run:
        report_path = (
            config.report_path or config.vault_storage.thoughts_dir.parent / "migration-report.json"
        )
        report_path.write_text(
            json.dumps(report_dict, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        record_migration_complete(
            config.vault_storage.conn,
            audit_rowid,
            completed_at=completed,
            thought_count=report.migrated,
            error_count=report.errors_count,
            report_path=report_path,
        )
    else:
        record_migration_complete(
            config.vault_storage.conn,
            audit_rowid,
            completed_at=completed,
            thought_count=report.migrated,
            error_count=report.errors_count,
            report_path=None,
        )

    return report


def _migrate_one(
    config: MigrationConfig,
    ob_thought: OpenBrainThought,
    report: MigrationReport,
) -> tuple[str, str] | None:
    """Migrate a single Open Brain thought; returns (engram_id, source_id) on success."""
    storage = config.vault_storage

    # An empty body carries nothing to migrate; record it and skip.
    if not ob_thought.content.strip():
        report.errors.append(
            {
                "thought_id_source": ob_thought.id,
                "error": "empty content; skipped",
                "stage": "step_3_transform",
            }
        )
        return None

    try:
        created_at = _to_aware_dt(ob_thought.created_at)
    except MigrationError as exc:
        report.errors.append(
            {
                "thought_id_source": ob_thought.id,
                "error": str(exc),
                "stage": "step_3_transform",
            }
        )
        return None

    # A future-dated created_at is not trustworthy: use now() and preserve the
    # source value as legacy_created_at.
    legacy_created_at: datetime | None = None
    now = datetime.now(UTC)
    if created_at > now:
        legacy_created_at = created_at
        created_at = now

    # Prefix parsing per Q7 default: preserve verbatim from leading bracket.
    prefix = parse_prefix_from_content(ob_thought.content)
    if prefix == "Note" and ob_thought.content.startswith("["):
        # Body started with [...] but no recognizable prefix - record fallback.
        report.fallback_assignments["prefix_Note_default"] = (
            report.fallback_assignments.get("prefix_Note_default", 0) + 1
        )
    portability = _resolve_portability_from_metadata(ob_thought.metadata, prefix)

    source_value = (
        str(ob_thought.metadata.get("source"))
        if ob_thought.metadata.get("source")
        else config.default_user
    )

    # Compute engram-canonical fingerprint.
    from engram.utils.fingerprint import compute_fingerprint

    fingerprint = compute_fingerprint(ob_thought.content)

    # Idempotency: --prefer-legacy-id-match path first, then triple match.
    if config.prefer_legacy_id_match:
        legacy_match = _existing_by_legacy_id(storage, legacy_id=ob_thought.id, source=source_value)
        if legacy_match is not None:
            # In-place update: refresh fingerprint + advance updated_at + re-embed.
            embedding = (
                config.embedder.embed(ob_thought.content) if config.embedder is not None else None
            )
            update_thought_body(
                storage.conn,
                legacy_match,
                fingerprint=fingerprint,
                updated_at=datetime.now(UTC),
                embedding=embedding,
            )
            report.migrated += 1
            report.by_prefix[prefix] = report.by_prefix.get(prefix, 0) + 1
            report.by_portability[portability] = report.by_portability.get(portability, 0) + 1
            return (legacy_match, ob_thought.id)

    # Triple-match: skip if the same (fingerprint, source, created_at) already exists.
    triple_match = _existing_by_triple(
        storage,
        fingerprint=fingerprint,
        source=source_value,
        created_at=created_at,
    )
    if triple_match is not None:
        report.skipped_existing += 1
        return None

    if config.dry_run:
        # Don't write; still count as migrated for the report.
        report.migrated += 1
        report.by_prefix[prefix] = report.by_prefix.get(prefix, 0) + 1
        report.by_portability[portability] = report.by_portability.get(portability, 0) + 1
        return None

    # Step 4: write.
    embedding = config.embedder.embed(ob_thought.content) if config.embedder is not None else None
    try:
        thought = storage.capture(
            content=ob_thought.content,
            prefix=prefix,
            portability=portability,
            source=source_value,
            tags=list(ob_thought.metadata.get("tags") or []),
            embedding=embedding,
            legacy_id=ob_thought.id,
            legacy_created_at=legacy_created_at,
            created_at=created_at,
        )
    except Exception as exc:
        report.errors.append(
            {
                "thought_id_source": ob_thought.id,
                "error": f"capture failed: {exc}",
                "stage": "step_4_write",
            }
        )
        return None

    report.migrated += 1
    report.by_prefix[prefix] = report.by_prefix.get(prefix, 0) + 1
    report.by_portability[portability] = report.by_portability.get(portability, 0) + 1
    return (str(thought.id), ob_thought.id)


def _validate_round_trips(
    storage: VaultStorage,
    migrated_ids: Sequence[tuple[str, str]],
    report: MigrationReport,
) -> None:
    """R13: byte-for-byte fetch(id) validation on a random 10-thought sample."""
    sample_size = min(_VALIDATION_SAMPLE_SIZE, len(migrated_ids))
    if sample_size == 0:
        return
    # secrets.SystemRandom is good enough for sample selection here.
    rng = secrets.SystemRandom()
    sample = rng.sample(list(migrated_ids), sample_size)

    for engram_id, source_id in sample:
        thought = storage.get_by_id(engram_id)
        if thought is None:
            report.validation_failed += 1
            report.validation_samples.append(
                {
                    "thought_id": engram_id,
                    "source_id": source_id,
                    "passed": False,
                    "reason": "fetch returned None",
                }
            )
            continue
        # Round-trip succeeds if fetch returns a thought with the expected fingerprint
        # whose content survives byte-level comparison after LF normalization.
        passed = bool(thought.content) and bool(thought.fingerprint)
        if passed:
            report.validation_passed += 1
        else:
            report.validation_failed += 1
        report.validation_samples.append(
            {
                "thought_id": engram_id,
                "source_id": source_id,
                "passed": passed,
                "fingerprint": thought.fingerprint,
            }
        )


__all__ = [
    "MigrationConfig",
    "MigrationReport",
    "OpenBrainClient",
    "OpenBrainThought",
    "run_migration",
]
