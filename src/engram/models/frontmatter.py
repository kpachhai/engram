"""Frontmatter schema and the canonical engram prefix vocabulary.

The :class:`Frontmatter` model is the strict-validation Pydantic boundary for
parsed YAML frontmatter. It accepts (with a structured warning at the storage
layer, per ``02-TECHNICAL_DESIGN.md`` Frontmatter Schema Drift Handling) any
``prefix`` value, including non-canonical strings, but enforces:

* All required fields present (``id``, ``prefix``, ``portability``, ``source``,
  ``created_at``, ``updated_at``, ``fingerprint``).
* ``schema_version`` defaults to ``1`` when missing (NFR5 forward compat).
* ``portability`` is one of ``portable``, ``sensitive``, ``block``.
* ``created_at``, ``updated_at``, and ``legacy_created_at`` are timezone-aware.
* ``prefix`` does not contain path-traversal characters or RTL-override unicode.
* Unknown extra fields are preserved on the model so a write-side round-trip
  does not silently drop them.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The 15 canonical prefix values in their authoritative case-sensitive form.
#: Defined in ``02-TECHNICAL_DESIGN.md`` Canonical Prefix Vocabulary; this is the
#: single source of truth in code.
CANONICAL_PREFIXES: Final[tuple[str, ...]] = (
    "Lesson",
    "Pattern",
    "Decision",
    "Friction",
    "Resolution",
    "Action Item",
    "Parked",
    "Notice",
    "Domain",
    "Workflow",
    "Style",
    "Artifact",
    "Session Summary",
    "Meta",
    "Note",
)

#: Per-prefix default portability when the user does not specify one.
#: Most prefixes default to ``portable``; only the BYOC-sensitive layers
#: (``Domain`` and ``Artifact``) default to ``sensitive``.
DEFAULT_PORTABILITY_BY_PREFIX: Final[dict[str, str]] = {
    "Domain": "sensitive",
    "Artifact": "sensitive",
}

#: Type alias for the portability classification.
Portability = Literal["portable", "sensitive", "block"]

_PATH_TRAVERSAL_RE = re.compile(r"\.\.|\x00")
_RTL_OVERRIDE_CHARS = "\u202d\u202e\u200e\u200f\u061c"
_FINGERPRINT_HEX_RE = re.compile(r"^[a-f0-9]{64}$")


def is_canonical_prefix(prefix: str) -> bool:
    """Return ``True`` iff ``prefix`` is one of the 15 canonical engram prefixes."""
    return prefix in CANONICAL_PREFIXES


def _check_path_safe(value: str, kind: str) -> str:
    if _PATH_TRAVERSAL_RE.search(value):
        msg = f"{kind} contains path-traversal characters: {value!r}"
        raise ValueError(msg)
    if any(ch in _RTL_OVERRIDE_CHARS for ch in value):
        msg = f"{kind} contains right-to-left override characters: {value!r}"
        raise ValueError(msg)
    return value


class Frontmatter(BaseModel):
    """YAML frontmatter for a thought markdown file."""

    model_config = ConfigDict(
        extra="allow",
        validate_assignment=True,
        str_strip_whitespace=False,
    )

    schema_version: int = Field(default=1, ge=1)
    id: UUID
    prefix: str = Field(min_length=1, max_length=128)
    portability: Portability = "portable"
    source: str = Field(min_length=1, max_length=256)
    created_at: datetime
    updated_at: datetime
    fingerprint: str
    tags: list[str] = Field(default_factory=list)
    vault: str | None = None
    legacy_id: str | None = None
    legacy_created_at: datetime | None = None
    #: Phase 4: GPG primary-key fingerprint (40 hex) of the capturing user
    #: when the thought lives in a team-write vault. None for personal /
    #: read-only friend vaults (matches Phase 1+2+3 frontmatter shape).
    captured_by: str | None = None

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, value: str) -> str:
        return _check_path_safe(value, "prefix")

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        return _check_path_safe(value, "source")

    @field_validator("fingerprint")
    @classmethod
    def _check_fingerprint(cls, value: str) -> str:
        if not _FINGERPRINT_HEX_RE.fullmatch(value):
            msg = f"fingerprint must be 64 lowercase hex characters: {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("created_at", "updated_at", "legacy_created_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            msg = "datetime must be timezone-aware (UTC)"
            raise ValueError(msg)
        return value

    @model_validator(mode="before")
    @classmethod
    def _default_schema_version(cls, data: object) -> object:
        """Per NFR5: missing schema_version is treated as 1."""
        if isinstance(data, dict) and "schema_version" not in data:
            data["schema_version"] = 1
        return data


__all__ = [
    "CANONICAL_PREFIXES",
    "DEFAULT_PORTABILITY_BY_PREFIX",
    "Frontmatter",
    "Portability",
    "is_canonical_prefix",
]
