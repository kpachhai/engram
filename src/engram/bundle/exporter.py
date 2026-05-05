"""Bundle exporter (Phase 3 Step 9).

Streams a tar.gz archive of a vault's thoughts plus a ``manifest.json`` to
disk. Per the Phase 3 plan:

* Each thought file's bytes-on-disk are checked against
  :data:`engram.bundle.format.MAX_PER_FILE_BYTES` (1 MB) and refused
  with :class:`engram.errors.BundleImportError` if oversized.
* A rolling counter sums the cumulative bytes-written; if it would
  exceed :data:`engram.bundle.format.MAX_TOTAL_BYTES` (4 GB) the export
  aborts before the next file write.
* Only thoughts whose ``portability`` is in the requested filter list
  are included. The default filter is ``["portable"]``; the CLI
  command exposes a repeatable ``--portability`` flag (Plan NH-5).
* Atomic via temp-then-rename: writes go to ``<output>.tmp``; on
  successful close the temp file is renamed onto ``<output>``.
* The manifest is written LAST inside the tar so a partial bundle
  (writer crashed mid-stream) lacks a manifest. The importer's first
  read is the manifest; missing manifest -> hard refuse without
  opening any other member.

This skips git-related side effects entirely - a bundle is a
point-in-time snapshot. Live updates use ``engram clone-vault`` (Phase
2 deliverable) plus pull, not bundle re-export.
"""

from __future__ import annotations

import contextlib
import io
import logging
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from uuid_extensions import uuid7

from engram.bundle.format import (
    BUNDLE_MANIFEST_FILENAME,
    BUNDLE_THOUGHTS_DIR,
    MAX_PER_FILE_BYTES,
    MAX_TOTAL_BYTES,
    BundleManifest,
    now_utc,
)
from engram.errors import BundleImportError, VaultError
from engram.models.frontmatter import Portability

if TYPE_CHECKING:
    from uuid import UUID

    from engram.storage.facade import VaultStorage

_log = logging.getLogger("engram.bundle.exporter")


@dataclass(slots=True)
class BundleExportResult:
    """Summary returned by :meth:`BundleExporter.export_to`."""

    bundle_path: Path
    manifest: BundleManifest
    bytes_written: int = 0
    skipped_oversized: list[str] = field(default_factory=list)
    skipped_outside_filter: int = 0


class BundleExporter:
    """Stream a vault's portable thoughts into a tar.gz bundle.

    Construction is cheap; call :meth:`export_to` to actually write.
    """

    def __init__(
        self,
        *,
        storage: VaultStorage,
        portability_filter: Iterable[Portability] = ("portable",),
        source_user: str = "engram-user",
        embedding_model: str | None = None,
    ) -> None:
        """Bind exporter to a source vault + portability filter."""
        self.storage = storage
        self.portability_filter: tuple[Portability, ...] = tuple(portability_filter)
        if not self.portability_filter:
            msg = "BundleExporter requires at least one portability tier in the filter"
            raise VaultError(msg)
        if "block" in self.portability_filter:
            msg = "BundleExporter refuses portability='block'; bundles are friend-share only"
            raise VaultError(msg)
        self.source_user = source_user
        self.embedding_model = embedding_model or "unknown"

    def _new_bundle_id(self) -> UUID:
        from uuid import UUID

        return UUID(str(uuid7()))

    def export_to(self, output_path: Path | str) -> BundleExportResult:
        """Write the bundle to ``output_path`` and return a summary.

        The output path must NOT already exist (refuses to overwrite). On
        success the file is renamed atomically from ``<path>.tmp``.

        Raises:
            BundleImportError: when a single thought file exceeds
                :data:`MAX_PER_FILE_BYTES` or the cumulative bundle size
                would exceed :data:`MAX_TOTAL_BYTES`.
            VaultError: when the output path already exists.
        """
        output_path = Path(output_path).expanduser()
        if output_path.exists():
            msg = (
                f"refusing to overwrite existing bundle path: {output_path}; "
                "remove it or pick a different --output"
            )
            raise VaultError(msg)

        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        if tmp_path.exists():
            tmp_path.unlink()

        bundle_id = self._new_bundle_id()
        # Iterate eligible thoughts FIRST so the manifest's thought_count
        # is accurate and the size accounting can fail cleanly.
        thoughts, skipped_outside_filter = self._collect_eligible_thoughts()

        manifest = BundleManifest(
            schema_version=1,
            source_user=self.source_user,
            source_vault=self.storage.vault_name,
            exported_at=now_utc(),
            thought_count=len(thoughts),
            portability_filter=list(self.portability_filter),
            embedding_model=self.embedding_model,
            bundle_id=bundle_id,
        )

        result = BundleExportResult(
            bundle_path=output_path,
            manifest=manifest,
            skipped_outside_filter=skipped_outside_filter,
        )

        try:
            with tarfile.open(str(tmp_path), mode="w|gz") as tar:
                for rel_path, abs_path in thoughts:
                    file_size = abs_path.stat().st_size
                    if file_size > MAX_PER_FILE_BYTES:
                        result.skipped_oversized.append(str(rel_path))
                        _log.warning(
                            "bundle export skipping oversized file %s (%d bytes > %d limit)",
                            rel_path,
                            file_size,
                            MAX_PER_FILE_BYTES,
                        )
                        continue
                    if result.bytes_written + file_size > MAX_TOTAL_BYTES:
                        msg = (
                            f"bundle export would exceed {MAX_TOTAL_BYTES} bytes "
                            f"after adding {rel_path}; aborting"
                        )
                        raise BundleImportError(msg)
                    member_name = f"{BUNDLE_THOUGHTS_DIR}/{rel_path.as_posix()}"
                    tar.add(str(abs_path), arcname=member_name)
                    result.bytes_written += file_size

                manifest_bytes = manifest.to_json().encode("utf-8")
                manifest_info = tarfile.TarInfo(name=BUNDLE_MANIFEST_FILENAME)
                manifest_info.size = len(manifest_bytes)
                manifest_info.mtime = int(manifest.exported_at.timestamp())
                tar.addfile(manifest_info, _bytes_io(manifest_bytes))
                result.bytes_written += len(manifest_bytes)
        except BaseException:
            with _suppress_oserror():
                tmp_path.unlink(missing_ok=True)
            raise

        tmp_path.rename(output_path)
        return result

    def _collect_eligible_thoughts(self) -> tuple[list[tuple[Path, Path]], int]:
        """Iterate the storage and pick thoughts in the portability filter.

        Returns ``(eligible, skipped_count)`` where ``eligible`` is a
        list of ``(repo_relative_path, absolute_path)`` tuples and the
        skipped count is non-eligible thoughts (typically ``block`` or
        ``sensitive`` rows that fall outside the filter).
        """
        from engram.models.mcp import Filter  # noqa: F401

        eligible: list[tuple[Path, Path]] = []
        skipped = 0
        # ``list_thoughts`` paginates; iterate in chunks of 500 so very
        # large vaults still work without loading the whole row set.
        offset = 0
        page_size = 500
        while True:
            rows, total = self.storage.list_thoughts(
                limit=page_size,
                offset=offset,
                filter_=None,
                sort="created_at_asc",
            )
            for thought in rows:
                if thought.portability not in self.portability_filter:
                    skipped += 1
                    continue
                abs_path = thought.file_path
                if not abs_path.exists():
                    skipped += 1
                    continue
                rel_path = abs_path.relative_to(self.storage.thoughts_dir)
                eligible.append((rel_path, abs_path))
            offset += len(rows)
            if offset >= total or not rows:
                break
        return eligible, skipped


def _bytes_io(data: bytes) -> io.BytesIO:
    """Return a binary file-like object suitable for tarfile.addfile."""
    return io.BytesIO(data)


def _suppress_oserror() -> contextlib.AbstractContextManager[None]:
    """contextlib.suppress(OSError) without forcing module-level import."""
    return contextlib.suppress(OSError)
