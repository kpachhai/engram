r"""Filename derivation for thoughts.

Pattern::

    {prefix-dir}/{YYYYMMDDHHMMSS}-{slug}-{shortuuid12}.md

Where:

* ``prefix-dir`` is the prefix value lowercased with internal whitespace replaced
  by hyphens (``"Action Item"`` becomes ``"action-item"``).
* ``YYYYMMDDHHMMSS`` is the UTC ``created_at`` with no separators.
* ``slug`` is derived from the first 30 characters of the body, lowercased,
  with non-alphanumeric runs collapsed to ``-`` and leading/trailing ``-``
  stripped. Falls back to the literal string ``thought`` when the result is
  empty or all-hyphens.
* ``shortuuid12`` is the LAST 12 hex characters of the UUID-v7 (the random
  tail; not the timestamp prefix). 48 bits of entropy give a roughly
  16M-capture birthday-collision threshold.

Path components are validated against path-traversal hints (``..``, ``\x00``)
and right-to-left override unicode characters.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from uuid import UUID

from engram.errors import VaultError

_SLUG_LENGTH = 30
_UUID_TAIL_LENGTH = 12
_SLUG_FALLBACK = "thought"

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_PREFIX_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_PATH_TRAVERSAL_RE = re.compile(r"\.\.|\x00")
_RTL_OVERRIDE_CHARS = "\u202d\u202e\u200e\u200f\u061c"


def _validate_no_path_escape(component: str, kind: str) -> None:
    if _PATH_TRAVERSAL_RE.search(component):
        msg = f"{kind} contains path-traversal characters: {component!r}"
        raise VaultError(msg)
    if any(ch in _RTL_OVERRIDE_CHARS for ch in component):
        msg = f"{kind} contains right-to-left override characters: {component!r}"
        raise VaultError(msg)


def derive_prefix_dirname(prefix: str) -> str:
    """Derive the prefix subdirectory name from a frontmatter ``prefix`` value.

    Lowercase, NFKC-normalize, then collapse non-alphanumeric runs into ``-``
    and trim. Raises :class:`VaultError` if the result is empty or contains
    path-traversal characters.
    """
    if not prefix:
        msg = "prefix cannot be empty"
        raise VaultError(msg)
    _validate_no_path_escape(prefix, "prefix")

    lowered = unicodedata.normalize("NFKC", prefix).strip().lower()
    sanitized = _PREFIX_NORMALIZE_RE.sub("-", lowered).strip("-")
    if not sanitized:
        msg = f"prefix yields empty directory name after sanitization: {prefix!r}"
        raise VaultError(msg)
    return sanitized


def derive_slug(body: str) -> str:
    """Derive a filename slug from the leading body content.

    Returns at most ``_SLUG_LENGTH`` characters. Falls back to ``"thought"``
    when the result is empty or contains no alphanumeric characters.
    """
    if not body:
        return _SLUG_FALLBACK

    head = unicodedata.normalize("NFKC", body)
    head = head.lower()[:_SLUG_LENGTH]
    slug = _NON_ALNUM_RE.sub("-", head).strip("-")
    if not slug:
        return _SLUG_FALLBACK
    return slug


def derive_uuid_tail(thought_id: UUID) -> str:
    """Return the LAST ``_UUID_TAIL_LENGTH`` (12) hex chars of the UUID-v7.

    These bits come from the UUID's pseudo-random tail, NOT the timestamp
    prefix - so two captures in the same millisecond do not collide.
    """
    hex_str = thought_id.hex
    if len(hex_str) != 32:
        msg = f"unexpected UUID hex length: {hex_str!r}"
        raise VaultError(msg)
    return hex_str[-_UUID_TAIL_LENGTH:].lower()


def derive_relative_path(
    *,
    prefix: str,
    body: str,
    created_at: datetime,
    thought_id: UUID,
) -> Path:
    """Return the thought's vault-relative path.

    Format: ``{prefix-dir}/{YYYYMMDDHHMMSS}-{slug}-{shortuuid12}.md``.

    Args:
        prefix: Frontmatter prefix value (e.g. ``"Lesson"``, ``"Action Item"``).
        body: The thought body. The leading ``[Prefix]`` line, if present, is
            included; this matches the spec's slug derivation rule.
        created_at: Timezone-aware UTC datetime. Naive datetimes are rejected.
        thought_id: A UUID-v7. The last 12 hex characters become the
            collision-resistant filename tail.

    Raises:
        VaultError: if prefix is invalid, ``created_at`` is naive, or any
            component contains path-traversal characters.
    """
    if created_at.tzinfo is None:
        msg = "created_at must be timezone-aware (UTC)"
        raise VaultError(msg)

    prefix_dir = derive_prefix_dirname(prefix)
    timestamp = created_at.strftime("%Y%m%d%H%M%S")
    slug = derive_slug(body)
    tail = derive_uuid_tail(thought_id)
    filename = f"{timestamp}-{slug}-{tail}.md"
    return Path(prefix_dir) / filename


__all__ = [
    "derive_prefix_dirname",
    "derive_relative_path",
    "derive_slug",
    "derive_uuid_tail",
]
