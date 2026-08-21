"""MembersList - one fingerprint-per-line YAML enrolled-member roster.

The members file lives at ``<vault>/.engram/members.yaml`` checked into
the team's git remote. The format is intentionally minimal so concurrent
admins adding members produce line-level merge conflicts rather than
structured-tree conflicts.

Format (YAML):

    members:
      - 1234567890ABCDEF1234567890ABCDEF12345678  # pii-allow: synthetic
      - fingerprint: ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD  # pii-allow: synthetic
        display_name: alice
        superseded_by: NEW9999NEW9999NEW9999NEW9999NEW9999NEW9
    revoked:
      - DEADBEEF...

Each entry is either a bare fingerprint (40 hex; primary GPG key) or a
``{fingerprint, display_name?, superseded_by?}`` dict. The
``superseded_by`` field maps an old key to a new one so historical
thoughts under the old fingerprint stay attributed to the same display
name (per Q6 default).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_FINGERPRINT_RE = re.compile(r"^[A-F0-9]{40}$")  # vocab-allow: hex char class


def normalize_fingerprint(fp: str) -> str:
    """Return the fingerprint upper-cased + stripped of separators.

    Accepts both ``40-hex`` and ``16-hex short`` forms; canonicalizes
    to upper-case alphanumeric. Caller MUST validate via
    :func:`is_valid_fingerprint` before treating the result as canonical.
    """
    return fp.upper().replace(" ", "").replace(":", "")


def is_valid_fingerprint(fp: str) -> bool:
    """Return True if ``fp`` is a valid 40-hex GPG primary fingerprint."""
    return bool(_FINGERPRINT_RE.fullmatch(normalize_fingerprint(fp)))


class MemberEntry(BaseModel):
    """One enrolled member: fingerprint + optional display name + supersession."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=40, max_length=40)
    display_name: str | None = None
    superseded_by: str | None = None

    @field_validator("fingerprint", "superseded_by")
    @classmethod
    def _check_fingerprint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not is_valid_fingerprint(value):
            msg = f"fingerprint must be 40 hex characters (got {value!r})"
            raise ValueError(msg)
        return normalize_fingerprint(value)


class MembersList(BaseModel):
    """The full enrolled-members roster of a team vault.

    The schema is intentionally minimal; the YAML file is line-level
    merge-conflict friendly so two admins running ``add-member``
    concurrently land cleanly via ``git pull --rebase``.
    """

    model_config = ConfigDict(extra="forbid")

    members: list[MemberEntry] = Field(default_factory=list)
    #: Fingerprints explicitly revoked. The committer-mismatch check on
    #: the server hook also rejects any commit signed by a revoked key.
    revoked: list[str] = Field(default_factory=list)

    @field_validator("revoked")
    @classmethod
    def _check_revoked(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for fp in value:
            if not is_valid_fingerprint(fp):
                msg = f"revoked fingerprint must be 40 hex characters (got {fp!r})"
                raise ValueError(msg)
            normalized.append(normalize_fingerprint(fp))
        return normalized

    def is_enrolled(self, fingerprint: str) -> bool:
        """Return True iff ``fingerprint`` is enrolled and not revoked."""
        canonical = normalize_fingerprint(fingerprint)
        if canonical in self.revoked:
            return False
        return any(m.fingerprint == canonical for m in self.members)

    def display_name_of(self, fingerprint: str) -> str | None:
        """Return the display name for ``fingerprint`` (or None)."""
        canonical = normalize_fingerprint(fingerprint)
        for m in self.members:
            if m.fingerprint == canonical:
                return m.display_name
        return None

    @classmethod
    def from_yaml_dict(cls, data: dict[str, Any]) -> MembersList:
        """Parse a YAML-decoded dict into a MembersList.

        Tolerates the bare-string member form so a hand-edited file with
        just ``- ABCDEF...`` parses cleanly.
        """
        raw_members = data.get("members", []) or []
        normalized_members: list[dict[str, Any]] = []
        for entry in raw_members:
            if isinstance(entry, str):
                normalized_members.append({"fingerprint": entry})
            elif isinstance(entry, dict):
                normalized_members.append(entry)
            else:
                msg = f"member entry must be a string or dict; got {type(entry).__name__}"
                raise ValueError(msg)
        return cls(members=normalized_members, revoked=data.get("revoked", []) or [])  # type: ignore[arg-type]


__all__ = [
    "MemberEntry",
    "MembersList",
    "is_valid_fingerprint",
    "normalize_fingerprint",
]
