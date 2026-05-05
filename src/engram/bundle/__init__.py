"""Bundle export/import (friend-share via snapshots, not git-pull).

Friend-share runs through ``engram export`` -> transport channel ->
``engram import``, NOT live git-pull from a friend's vault. A friend's
git history is attacker-influenceable, and the bundle import gate is the
only place ``06-SECURITY.md`` lines 31-44 (path-traversal refusal,
per-file 1 MB, per-bundle 4 GB streaming, ``yaml.safe_load`` only,
id-collision refusal, ``portability=block`` filtering) can be applied
to friend content.

This package surfaces three things:

* :class:`BundleManifest` (in :mod:`format`): the on-disk
  ``manifest.json`` schema, schema_version=1.
* :class:`BundleExporter` (in :mod:`exporter`): streams a tar.gz of
  ``thoughts/`` + ``manifest.json`` to disk under per-file and total
  size caps; manifest is written LAST so a partial bundle has no
  manifest and is detectable.
* :class:`BundleImporter` (in :mod:`importer`): the bundle reception
  gate; staged-then-merged with id-collision pre-flight + bundle_id
  cycle detection.
"""

from __future__ import annotations

from engram.bundle.exporter import BundleExporter, BundleExportResult
from engram.bundle.format import (
    BUNDLE_MANIFEST_FILENAME,
    BUNDLE_SCHEMA_VERSION,
    BUNDLE_THOUGHTS_DIR,
    MAX_PER_FILE_BYTES,
    MAX_TOTAL_BYTES,
    BundleManifest,
)
from engram.bundle.importer import BundleImporter, BundleImportResult

__all__ = [
    "BUNDLE_MANIFEST_FILENAME",
    "BUNDLE_SCHEMA_VERSION",
    "BUNDLE_THOUGHTS_DIR",
    "MAX_PER_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "BundleExportResult",
    "BundleExporter",
    "BundleImportResult",
    "BundleImporter",
    "BundleManifest",
]
