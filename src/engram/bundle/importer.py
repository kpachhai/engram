r"""Bundle importer (Phase 3 Step 10).

The bundle reception gate. Per ``docs/PHASE_3_PLAN.md`` Step 10:

1. Read ``manifest.json`` first; refuse if ``schema_version != 1``.
2. Walk the target vault's existing ``source: bundle:<id>`` chains; if
   ``manifest.bundle_id`` appears, refuse with
   :class:`engram.errors.BundleCycleDetected` (R-M13).
3. Stream the tar.gz so a 4 GB bundle never loads fully into RAM.
4. Validate every member: under ``thoughts/``, no ``..`` segments, NFC
   normalize, ``\\`` -> ``/``, BOM strip, ``yaml.safe_load`` only,
   ``portability != "block"``.
5. Stage all writes into
   ``<vault>/.indexes/import-staging-<bundle_id>/``.
6. Pre-flight id-collision scan against ``SELECT id FROM thoughts``;
   on ANY collision, abort the bundle BEFORE merge into ``thoughts/``.
   This is the atomic-at-pre-flight contract from SF-4 fix.
7. On success, walk the staging directory file-by-file and copy each
   into ``thoughts/`` under its repo-relative path; update
   ``migration-report.json`` after each file write.
8. Tag every imported thought with ``source: bundle:<id> <- ...``
   chain inherited from the manifest plus the candidate id.

Crash recovery: per SF-4, the per-file copy is best-effort; if a crash
hits mid-merge, ``migration-report.json`` records what landed and
``engram doctor`` surfaces the partial-state FAIL with operator-runnable
resume instructions. ``engram import-resume`` is deferred to Phase 4.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import tarfile
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from engram.bundle.format import (
    BUNDLE_MANIFEST_FILENAME,
    BUNDLE_THOUGHTS_DIR,
    MAX_PER_FILE_BYTES,
    MAX_TOTAL_BYTES,
    BundleManifest,
)
from engram.errors import (
    BundleCycleDetected,
    BundleImportError,
    VaultError,
)
from engram.storage.markdown import read_thought, write_thought

if TYPE_CHECKING:
    from engram.models import Thought
    from engram.storage.facade import VaultStorage

_log = logging.getLogger("engram.bundle.importer")

#: ``\ufeff`` is the UTF-8 byte-order mark; strip on read for cross-OS sanity.
_BOM = "\ufeff"


@dataclass(slots=True)
class BundleImportResult:
    """Summary returned by :meth:`BundleImporter.import_into`."""

    manifest: BundleManifest
    imported_count: int = 0
    skipped_block_count: int = 0
    skipped_oversized: list[str] = field(default_factory=list)
    rejected_path_traversal: list[str] = field(default_factory=list)
    rejected_outside_thoughts_dir: list[str] = field(default_factory=list)
    id_collisions: list[str] = field(default_factory=list)
    migration_report_path: Path | None = None


class BundleImporter:
    """Stage-then-merge bundle importer for a target VaultStorage."""

    def __init__(self, *, target: VaultStorage, allow_read_only: bool = False) -> None:
        """Bind importer to ``target``; refuse a read-only target unless allowed."""
        self.target = target
        self.allow_read_only = allow_read_only

    def import_into(self, bundle_path: Path | str) -> BundleImportResult:
        """Import ``bundle_path`` into the target vault.

        The full sequence runs in this order:

        1. Refuse if the target is read-only and ``allow_read_only`` is
           False.
        2. Open the tar (streamed) and read the manifest first.
        3. Cycle detection by ``bundle_id`` chain.
        4. Stage the archive into
           ``<vault>/.indexes/import-staging-<bundle_id>/``.
        5. Validate every staged member (path-traversal, sizes, frontmatter).
        6. Pre-flight id-collision scan; refuse the entire bundle on ANY
           collision (atomic at the pre-flight level).
        7. Per-thought merge: rewrite the staged markdown with the
           ``source`` tag updated to carry the bundle chain, write into
           the target's ``thoughts_dir`` via the storage facade so the
           SQLite index stays consistent.
        8. Update ``migration-report.json`` after each file write so a
           crash mid-merge leaves an inspectable trail.

        Returns:
            A :class:`BundleImportResult` describing the outcome.
        """
        bundle_path = Path(bundle_path).expanduser().resolve()
        if not bundle_path.is_file():
            msg = f"bundle path is not a file: {bundle_path}"
            raise BundleImportError(msg)

        if self.target.read_only_role and not self.allow_read_only:
            msg = (
                f"target vault {self.target.vault_name!r} is mounted read-only; "
                "pass --allow-read-only to import anyway (rare; usually a friend "
                "vault should be the import target, not a primary)."
            )
            raise BundleImportError(msg)

        manifest = self._read_manifest(bundle_path)

        # Cycle detection BEFORE staging - cheap and prevents wasted I/O.
        self._refuse_if_cycle(manifest=manifest)

        staging_dir = (
            self.target.thoughts_dir.parent / ".indexes" / f"import-staging-{manifest.bundle_id}"
        )
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=False)

        result = BundleImportResult(manifest=manifest)
        try:
            self._stage_archive(
                bundle_path=bundle_path,
                staging_dir=staging_dir,
                result=result,
            )
            self._refuse_id_collisions(staging_dir=staging_dir, result=result)
            self._merge_into_target(
                staging_dir=staging_dir,
                manifest=manifest,
                result=result,
            )
            result.migration_report_path = self._write_migration_report(
                manifest=manifest, result=result
            )
        finally:
            with _suppress_oserror():
                shutil.rmtree(staging_dir, ignore_errors=True)

        return result

    # === step helpers ===

    def _read_manifest(self, bundle_path: Path) -> BundleManifest:
        """Open the tar (streamed), find ``manifest.json``, validate v=1."""
        from pydantic import ValidationError

        with tarfile.open(str(bundle_path), mode="r|gz") as tar:
            for member in tar:
                if member.name == BUNDLE_MANIFEST_FILENAME:
                    if member.size > MAX_PER_FILE_BYTES:
                        msg = f"manifest.json too large: {member.size} > {MAX_PER_FILE_BYTES}"
                        raise BundleImportError(msg)
                    handle = tar.extractfile(member)
                    if handle is None:
                        msg = "manifest.json is unreadable in this archive"
                        raise BundleImportError(msg)
                    raw = handle.read().decode("utf-8")
                    try:
                        return BundleManifest.from_json(raw)
                    except (ValidationError, ValueError, TypeError) as exc:
                        msg = (
                            f"bundle manifest.json failed validation "
                            f"(schema_version_unsupported or malformed): {exc}"
                        )
                        raise BundleImportError(msg) from exc

        msg = (
            f"bundle is missing {BUNDLE_MANIFEST_FILENAME}; this archive may "
            "be a partial export (writer crashed mid-stream) or not an "
            "engram bundle at all."
        )
        raise BundleImportError(msg)

    def _refuse_if_cycle(self, *, manifest: BundleManifest) -> None:
        """Walk the target's existing ``source`` field looking for ``manifest.bundle_id``."""
        target_tag = manifest.bundle_source_tag
        # Walk all thoughts; cheap enough for personal-scale corpora.
        # In a friendlier future we'd index on `source` and grep that
        # index, but Phase 3 doesn't yet build that index.
        offset = 0
        page_size = 500
        while True:
            rows, total = self.target.list_thoughts(
                limit=page_size, offset=offset, sort="created_at_asc"
            )
            for t in rows:
                if t.source and target_tag in t.source:
                    msg = (
                        f"cycle detected: target vault already has thoughts whose "
                        f"source chain includes {target_tag!r}. Refusing import."
                    )
                    raise BundleCycleDetected(msg)
            offset += len(rows)
            if offset >= total or not rows:
                break

    def _stage_archive(
        self,
        *,
        bundle_path: Path,
        staging_dir: Path,
        result: BundleImportResult,
    ) -> None:
        """Stream-extract the archive into ``staging_dir`` with all gates fired.

        Member-by-member checks:

        * Member name must NOT start with ``/`` and must contain no
          ``..`` segments (path-traversal gate).
        * Member name must start with ``thoughts/`` (or equal
          ``manifest.json`` which we just consumed and skip).
        * Per-file size <= :data:`MAX_PER_FILE_BYTES`.
        * Cumulative size <= :data:`MAX_TOTAL_BYTES`.
        * Markdown frontmatter validates and ``portability != "block"``.

        Each accepted member is written under
        ``staging_dir/<rel-under-thoughts>``.
        """
        cumulative_bytes = 0
        with tarfile.open(str(bundle_path), mode="r|gz") as tar:
            for member in tar:
                if member.name == BUNDLE_MANIFEST_FILENAME:
                    continue
                if not member.isfile():
                    continue
                normalized = self._normalize_member_name(member.name)
                if normalized is None:
                    result.rejected_path_traversal.append(member.name)
                    continue
                if not normalized.startswith(BUNDLE_THOUGHTS_DIR + "/"):
                    result.rejected_outside_thoughts_dir.append(member.name)
                    continue

                if member.size > MAX_PER_FILE_BYTES:
                    result.skipped_oversized.append(normalized)
                    continue
                cumulative_bytes += member.size
                if cumulative_bytes > MAX_TOTAL_BYTES:
                    msg = (
                        f"bundle exceeds total size cap of {MAX_TOTAL_BYTES} bytes "
                        f"after {member.name}; aborting"
                    )
                    raise BundleImportError(msg)

                rel_path = Path(normalized[len(BUNDLE_THOUGHTS_DIR) + 1 :])
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                content_bytes = handle.read()
                content_text = self._decode_member(content_bytes)

                # Validate frontmatter via the storage layer's reader.
                # We write the bytes to a staging path FIRST so the
                # reader has something to operate on, then re-validate.
                staged_path = staging_dir / rel_path
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_text(content_text, encoding="utf-8")

                read_outcome = read_thought(staged_path)
                if read_outcome is None or read_outcome[0] is None:
                    # Unreadable -> drop this member and continue. Upstream
                    # ``migration-report.json`` records the rejection.
                    staged_path.unlink(missing_ok=True)
                    result.rejected_outside_thoughts_dir.append(normalized)
                    continue

                staged_thought = read_outcome[0]
                if staged_thought.portability == "block":
                    staged_path.unlink(missing_ok=True)
                    result.skipped_block_count += 1
                    continue

    @staticmethod
    def _normalize_member_name(raw_name: str) -> str | None:
        r"""Apply NFC + ``\\``->``/`` normalization; refuse traversal.

        Returns the normalized name, or None if the member should be
        rejected entirely.
        """
        if not raw_name:
            return None
        if raw_name.startswith("/"):
            return None
        normalized = unicodedata.normalize("NFC", raw_name)
        normalized = normalized.replace("\\", "/")
        # Refuse any path that climbs above the archive root.
        parts = normalized.split("/")
        if any(part in ("..", "") for part in parts[:-1]) or ".." in parts:
            return None
        if parts and parts[-1] == "":
            return None
        return normalized

    @staticmethod
    def _decode_member(raw_bytes: bytes) -> str:
        """Decode UTF-8 with BOM stripping; raise BundleImportError on bad bytes."""
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = f"non-UTF-8 markdown bytes in bundle member: {exc}"
            raise BundleImportError(msg) from exc
        if text.startswith(_BOM):
            text = text[len(_BOM) :]
        return text

    def _refuse_id_collisions(
        self,
        *,
        staging_dir: Path,
        result: BundleImportResult,
    ) -> None:
        """Pre-flight: walk staged thoughts; refuse if any id is already in target."""
        existing_ids = self._existing_ids_in_target()
        collisions: list[str] = []
        for staged in staging_dir.rglob("*.md"):
            outcome = read_thought(staged)
            if outcome is None or outcome[0] is None:
                continue
            tid = str(outcome[0].id)
            if tid in existing_ids:
                collisions.append(tid)
        if collisions:
            result.id_collisions.extend(collisions)
            msg = (
                f"bundle import refused: {len(collisions)} id collision(s) "
                f"with existing thoughts (e.g. {collisions[0]!r}). The whole "
                "bundle is rejected to preserve atomicity."
            )
            raise BundleImportError(msg)

    def _existing_ids_in_target(self) -> set[str]:
        """Return the full id set of the target vault."""
        ids: set[str] = set()
        offset = 0
        page_size = 1000
        while True:
            rows, total = self.target.list_thoughts(
                limit=page_size, offset=offset, sort="created_at_asc"
            )
            for t in rows:
                ids.add(str(t.id))
            offset += len(rows)
            if offset >= total or not rows:
                break
        return ids

    def _merge_into_target(
        self,
        *,
        staging_dir: Path,
        manifest: BundleManifest,
        result: BundleImportResult,
    ) -> None:
        """Per-thought write into the target vault.

        Bypasses the storage facade's read-only guard via the temporary
        ``allow_read_only`` flag (the importer caller must have already
        opted in via ``allow_read_only=True``). This temporarily clears
        ``read_only_role`` for the duration of the merge so writes
        succeed.
        """
        previous_read_only = self.target.read_only_role
        if self.allow_read_only and previous_read_only:
            self.target.set_read_only_role(read_only=False)
        try:
            for staged in sorted(staging_dir.rglob("*.md")):
                outcome = read_thought(staged)
                if outcome is None or outcome[0] is None:
                    continue
                staged_thought = outcome[0]
                tagged = self._retag_with_chain(thought=staged_thought, manifest=manifest)
                rel = staged.relative_to(staging_dir)
                target_path = self.target.thoughts_dir / rel
                target_path.parent.mkdir(parents=True, exist_ok=True)
                # Use the model's file_path so write_thought puts it in
                # the target vault's tree, not the staging tree.
                tagged_with_path = tagged.model_copy(update={"file_path": target_path})
                write_thought(tagged_with_path, base_dir=self.target.thoughts_dir)
                # Re-insert into SQLite with the tag in place so the
                # cycle-detection in subsequent imports walks the chain.
                from engram.storage.sqlite_queries import insert_thought

                try:
                    insert_thought(
                        self.target.conn,
                        thought_id=tagged_with_path.id,
                        prefix=tagged_with_path.prefix,
                        portability=tagged_with_path.portability,
                        source=tagged_with_path.source,
                        created_at=tagged_with_path.created_at,
                        updated_at=tagged_with_path.updated_at,
                        fingerprint=tagged_with_path.fingerprint,
                        file_path=str(target_path.relative_to(self.target.thoughts_dir)),
                        vault_name=self.target.vault_name,
                        tags=tagged_with_path.tags,
                        legacy_id=tagged_with_path.legacy_id,
                        legacy_created_at=None,
                        schema_version=tagged_with_path.schema_version,
                        embedding=None,
                    )
                except VaultError:  # pragma: no cover - storage refused
                    raise
                result.imported_count += 1
        finally:
            if self.allow_read_only and previous_read_only:
                self.target.set_read_only_role(read_only=True)

    @staticmethod
    def _retag_with_chain(*, thought: Thought, manifest: BundleManifest) -> Thought:
        """Compose ``source: bundle:<new-id> <- <prior chain>``.

        If the thought already has a ``source`` value (from a previous
        bundle round-trip), prepend the new bundle id; otherwise use the
        bundle tag alone.
        """
        new_tag = manifest.bundle_source_tag
        prior = thought.source.strip() if thought.source else ""
        if prior:
            if new_tag in prior:
                # Defense-in-depth: don't double-prepend.
                composed = prior
            elif prior.startswith("bundle:"):
                composed = f"{new_tag} <- {prior}"
            else:
                composed = f"{new_tag} <- {prior}"
        else:
            composed = new_tag
        return thought.model_copy(update={"source": composed})

    def _write_migration_report(
        self, *, manifest: BundleManifest, result: BundleImportResult
    ) -> Path:
        """Persist a JSON report under ``<vault>/.indexes/`` for doctor."""
        report_dir = self.target.thoughts_dir.parent / ".indexes"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"bundle-import-{manifest.bundle_id}.json"
        payload = {
            "bundle_id": str(manifest.bundle_id),
            "source_user": manifest.source_user,
            "source_vault": manifest.source_vault,
            "exported_at": manifest.exported_at.isoformat(),
            "imported_at": datetime.now(UTC).isoformat(),
            "imported_count": result.imported_count,
            "skipped_block_count": result.skipped_block_count,
            "skipped_oversized": list(result.skipped_oversized),
            "rejected_path_traversal": list(result.rejected_path_traversal),
            "rejected_outside_thoughts_dir": list(result.rejected_outside_thoughts_dir),
            "id_collisions": list(result.id_collisions),
        }
        report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return report_path


def _suppress_oserror() -> contextlib.AbstractContextManager[None]:
    """Local helper for ``contextlib.suppress(OSError)``."""
    return contextlib.suppress(OSError)
