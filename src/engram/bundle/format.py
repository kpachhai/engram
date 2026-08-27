"""Bundle format Pydantic model + on-disk constants.

The on-disk format is a single ``.tar.gz`` containing:

* ``manifest.json`` at the archive root - this file. Read first; refused
  if ``schema_version`` is not 1.
* ``thoughts/<rel-path>.md`` - one markdown file per exported thought.
  ``<rel-path>`` mirrors the source vault's directory layout (e.g.
  ``Pattern/2026/05/05/<id>.md``); the importer treats this as
  data and re-anchors under the target vault's ``thoughts_dir``.

The bundle reception gate enforces:

* Per-file ceiling :data:`MAX_PER_FILE_BYTES` (1 MB).
* Total bundle ceiling :data:`MAX_TOTAL_BYTES` (4 GB).
* No ``..`` segments in member names; no absolute paths.
* ``yaml.safe_load`` only (already enforced upstream by the markdown
  reader; the importer additionally re-validates schema before merge).
* Refuse duplicate ``id`` against existing thoughts in the target
  vault.
* Reject members with ``portability: block`` (defense-in-depth - the
  exporter shouldn't include them but a malicious / mistaken friend
  push by hand still gets filtered here).

The bundle format is currently ``schema_version=1`` only; the importer
refuses anything else (forward-incompatible by design).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from engram.models.frontmatter import Portability

#: Per-file size ceiling enforced at both export and import sides.
MAX_PER_FILE_BYTES: int = 1 * 1024 * 1024  # 1 MB
#: Total bundle size ceiling. Enforced via streaming counter at import
#: side (tar header sizes are summed before extraction).
MAX_TOTAL_BYTES: int = 4 * 1024 * 1024 * 1024  # 4 GB
#: Required prefix for every member in the archive (path traversal gate).
BUNDLE_THOUGHTS_DIR: str = "thoughts"
#: Manifest filename at archive root.
BUNDLE_MANIFEST_FILENAME: str = "manifest.json"
#: Current schema version. Importer refuses anything else.
BUNDLE_SCHEMA_VERSION: int = 1


class BundleManifest(BaseModel):
    """Top-level metadata for a bundle.

    Stable shape for the v1 lifetime; field additions are non-breaking.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    #: Logical user identifier on the export side. Used for provenance,
    #: not for cycle detection (cycles are detected by ``bundle_id``
    #: chain so multi-machine same-user imports work).
    source_user: str = Field(min_length=1)
    #: Vault name on the export side (provenance only).
    source_vault: str = Field(min_length=1)
    #: ISO-8601 UTC timestamp of when the bundle was created.
    exported_at: datetime
    thought_count: int = Field(ge=0)
    portability_filter: list[Portability] = Field(
        default_factory=lambda: ["portable"]  # type: ignore[arg-type]
    )
    embedding_model: str = Field(min_length=1)
    #: UUID-v7 mint at export time. The importer walks every existing
    #: thought's ``source: bundle:<id>`` chain looking for this id; if
    #: present, it's a cycle and the bundle is refused.
    bundle_id: UUID

    @property
    def bundle_source_tag(self) -> str:
        """Return the ``source:`` value imported thoughts inherit."""
        return f"bundle:{self.bundle_id}"

    def to_json(self) -> str:
        """Render to a stable JSON string for the on-disk manifest."""
        # mode="json" emits ISO timestamps + UUIDs as strings.
        payload: dict[str, Any] = self.model_dump(mode="json")
        return json.dumps(payload, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> BundleManifest:
        """Parse a JSON manifest blob; raises ValidationError on bad input."""
        data = json.loads(raw)
        if not isinstance(data, dict):
            msg = f"manifest.json must be a JSON object, got {type(data).__name__}"
            raise TypeError(msg)
        return cls.model_validate(data)


def manifest_path(extracted_root: Path) -> Path:
    """Return the path to ``manifest.json`` under an extracted bundle root."""
    return extracted_root / BUNDLE_MANIFEST_FILENAME


def now_utc() -> datetime:
    """Wall-clock now, UTC. Test seam."""
    return datetime.now(UTC)
