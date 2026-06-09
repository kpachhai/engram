r"""Markdown source-of-truth layer for engram.

A thought is stored as one markdown file with YAML frontmatter and a body. This
module is the read/write boundary between disk and the typed :class:`Thought`
model. Writes are atomic (via :mod:`engram.utils.atomic_write`); reads tolerate
schema drift by returning a structured :class:`FrontmatterDrift` list rather
than raising, per ``02-TECHNICAL_DESIGN.md`` Frontmatter Schema Drift Handling.

Two-parse design: PyYAML ``safe_load`` for Pydantic-validated read, ruamel.yaml
round-trip for write-side preservation of unknown extra fields. This avoids the
``CommentedMap`` versus ``dict`` mismatch when handing parsed YAML to Pydantic
strict-mode validators.

Body extraction notes:

* Frontmatter is delimited by ``---`` lines at the top of the file. The
  closing ``---`` marks the body start. Any subsequent ``---`` in the body
  is preserved as content (A4 from the plan's edge-case enumeration).
* On write, line endings are normalized to LF (NFR4); the body string from
  the Thought model is the canonical form.
* On read, file bytes are UTF-8 decoded; non-UTF-8 produces a drift entry.

All writes use atomic-rename and mode 0600 per ``06-SECURITY.md`` Boundary B1.
"""

from __future__ import annotations

import enum
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from engram.models import Frontmatter, Thought, is_canonical_prefix
from engram.utils.atomic_write import atomic_write_text

_log = logging.getLogger("engram.storage.markdown")

_FRONTMATTER_FENCE = "---"
_FRONTMATTER_FENCE_NL = "---\n"


class DriftReason(enum.StrEnum):
    """Categories of frontmatter schema drift surfaced on read."""

    NO_FRONTMATTER = "no_frontmatter"
    YAML_PARSE_ERROR = "yaml_parse_error"
    NOT_UTF8 = "not_utf8"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    SCHEMA_VIOLATION = "schema_violation"
    UNKNOWN_PREFIX = "unknown_prefix"
    UNKNOWN_EXTRA_FIELD = "unknown_extra_field"
    DUPLICATE_ID = "duplicate_id"


@dataclass(frozen=True)
class FrontmatterDrift:
    """Single drift observation surfaced during a read attempt.

    The storage facade aggregates these for ``engram doctor`` reporting.
    """

    path: Path
    reason: DriftReason
    detail: str


_REQUIRED_FRONTMATTER_FIELDS = {
    "id",
    "prefix",
    "portability",
    "source",
    "created_at",
    "updated_at",
    "fingerprint",
}

_KNOWN_FRONTMATTER_FIELDS = _REQUIRED_FRONTMATTER_FIELDS | {
    "schema_version",
    "tags",
    "vault",
    "legacy_id",
    "legacy_created_at",
    # GPG primary-fingerprint of capturing user (team-write vaults).
    "captured_by",
    # Consolidation provenance (archived originals + merged thoughts).
    "archived_at",
    "superseded_by",
    "consolidated_from",
    "consolidated_range",
}


def split_frontmatter(content: str) -> tuple[str, str] | None:
    """Split a markdown file's text into (frontmatter_yaml, body).

    Returns ``None`` if the content does not begin with a ``---`` fence or the
    closing fence is missing. Body content may contain literal ``---`` lines;
    the splitter only consumes the FIRST closing ``---`` after the opening fence.
    """
    if not content.startswith(_FRONTMATTER_FENCE_NL):
        return None
    rest = content[len(_FRONTMATTER_FENCE_NL) :]
    end_idx = rest.find("\n" + _FRONTMATTER_FENCE_NL)
    if end_idx == -1:
        # Tolerate file ending exactly with "\n---" (no trailing newline).
        if rest.endswith("\n" + _FRONTMATTER_FENCE):
            return rest[: -(len(_FRONTMATTER_FENCE) + 1)], ""
        return None
    fm_yaml = rest[:end_idx]
    body = rest[end_idx + len(_FRONTMATTER_FENCE_NL) + 1 :]
    return fm_yaml, body


def _decode_file(path: Path) -> tuple[str | None, FrontmatterDrift | None]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _log.warning("failed to read %s: %s", path, exc)
        return None, None
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, FrontmatterDrift(
            path=path,
            reason=DriftReason.NOT_UTF8,
            detail=f"file is not valid UTF-8: {exc}",
        )


def _parse_frontmatter_yaml(
    fm_yaml: str, path: Path
) -> tuple[dict[str, Any] | None, FrontmatterDrift | None]:
    """Parse frontmatter YAML via safe_load semantics; never executes Python tags."""
    yaml_safe = YAML(typ="safe", pure=True)
    try:
        data = yaml_safe.load(io.StringIO(fm_yaml))
    except YAMLError as exc:
        return None, FrontmatterDrift(
            path=path,
            reason=DriftReason.YAML_PARSE_ERROR,
            detail=f"YAML parse failed: {exc}",
        )
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, FrontmatterDrift(
            path=path,
            reason=DriftReason.YAML_PARSE_ERROR,
            detail="frontmatter must be a YAML mapping",
        )
    return dict(data), None


def _classify_drift_from_validation_error(
    exc: ValidationError, path: Path
) -> list[FrontmatterDrift]:
    drifts: list[FrontmatterDrift] = []
    seen: set[tuple[str, str]] = set()
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc") or ())
        msg = err.get("msg", "validation failed")
        if err.get("type") == "missing":
            reason = DriftReason.MISSING_REQUIRED_FIELD
        else:
            reason = DriftReason.SCHEMA_VIOLATION
        key = (reason.value, loc)
        if key in seen:
            continue
        seen.add(key)
        detail = f"{loc}: {msg}" if loc else msg
        drifts.append(FrontmatterDrift(path=path, reason=reason, detail=detail))
    return drifts


def read_thought(
    file_path: Path,
) -> tuple[Thought | None, list[FrontmatterDrift]] | None:
    """Read a thought from disk.

    Returns:
        ``None`` if the file does not exist or has no frontmatter at all.
        Otherwise a tuple ``(thought, drifts)`` where ``thought`` is the
        validated :class:`Thought` (or ``None`` if drift prevented indexing)
        and ``drifts`` is a list of :class:`FrontmatterDrift` observations
        for the storage facade to log + report via ``engram doctor``.
    """
    if not file_path.exists():
        return None

    decoded, decode_drift = _decode_file(file_path)
    if decode_drift is not None:
        return None, [decode_drift]
    if decoded is None:
        return None

    split = split_frontmatter(decoded)
    if split is None:
        # File with no frontmatter at all -> log + skip per Schema Drift table.
        _log.warning("file has no frontmatter; skipping: %s", file_path)
        return None
    fm_yaml, body = split

    fm_dict, parse_drift = _parse_frontmatter_yaml(fm_yaml, file_path)
    if parse_drift is not None or fm_dict is None:
        return None, [parse_drift] if parse_drift is not None else []

    drifts: list[FrontmatterDrift] = []

    extra_fields = set(fm_dict.keys()) - _KNOWN_FRONTMATTER_FIELDS
    for field in sorted(extra_fields):
        drifts.append(
            FrontmatterDrift(
                path=file_path,
                reason=DriftReason.UNKNOWN_EXTRA_FIELD,
                detail=f"unknown frontmatter field preserved on round-trip: {field}",
            )
        )

    try:
        frontmatter = Frontmatter.model_validate(fm_dict)
    except ValidationError as exc:
        validation_drifts = _classify_drift_from_validation_error(exc, file_path)
        drifts.extend(validation_drifts)
        return None, drifts

    if not is_canonical_prefix(frontmatter.prefix):
        drifts.append(
            FrontmatterDrift(
                path=file_path,
                reason=DriftReason.UNKNOWN_PREFIX,
                detail=(
                    f"prefix {frontmatter.prefix!r} is not in the canonical 15-prefix vocabulary"
                ),
            )
        )

    thought = Thought.model_validate(
        {
            "id": frontmatter.id,
            "schema_version": frontmatter.schema_version,
            "prefix": frontmatter.prefix,
            "portability": frontmatter.portability,
            "source": frontmatter.source,
            "created_at": frontmatter.created_at,
            "updated_at": frontmatter.updated_at,
            "fingerprint": frontmatter.fingerprint,
            "tags": frontmatter.tags,
            "vault": frontmatter.vault or "default",
            "legacy_id": frontmatter.legacy_id,
            "captured_by": frontmatter.captured_by,
            "content": body,
            "file_path": file_path,
        }
    )
    return thought, drifts


def _serialize_frontmatter(
    thought: Thought,
    *,
    extras: dict[str, Any] | None = None,
) -> str:
    """Serialize a thought's frontmatter via ruamel round-trip; preserve unknown extras."""
    yaml_rt = YAML(typ="rt")
    yaml_rt.default_flow_style = False
    yaml_rt.allow_unicode = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)

    # Force-quote fields that could be misread as YAML scalars of another type:
    # - id (UUID with hex digits could parse as int if all-digit)
    # - fingerprint (SHA-256 with all-digit values would parse as int)
    # - created_at / updated_at (ISO-8601 strings; YAML may interpret as datetime).
    payload: dict[str, Any] = {
        "schema_version": thought.schema_version,
        "id": DoubleQuotedScalarString(str(thought.id)),
        "prefix": thought.prefix,
        "portability": thought.portability,
        "source": thought.source,
        "created_at": DoubleQuotedScalarString(thought.created_at.isoformat()),
        "updated_at": DoubleQuotedScalarString(thought.updated_at.isoformat()),
        "fingerprint": DoubleQuotedScalarString(thought.fingerprint),
    }
    if thought.tags:
        payload["tags"] = list(thought.tags)
    if thought.vault and thought.vault != "default":
        payload["vault"] = thought.vault
    if thought.legacy_id is not None:
        payload["legacy_id"] = thought.legacy_id
    # Emit captured_by only when populated (team-write captures);
    # personal-vault captures keep the original frontmatter shape.
    if thought.captured_by is not None:
        payload["captured_by"] = DoubleQuotedScalarString(thought.captured_by)

    if extras:
        for k, v in extras.items():
            if k not in payload:
                payload[k] = v

    buf = io.StringIO()
    yaml_rt.dump(payload, buf)
    return buf.getvalue()


#: Fields the serializer re-derives from the Thought on every write. Anything
#: else found in an existing file - unknown extras AND known fields not
#: carried on the Thought model (legacy_created_at, consolidation provenance)
#: - is preserved verbatim on rewrite, or it would be silently dropped by
#: update_metadata / update_body / reindex re-capture.
_SERIALIZER_OWNED_FIELDS = {
    "schema_version",
    "id",
    "prefix",
    "portability",
    "source",
    "created_at",
    "updated_at",
    "fingerprint",
    "tags",
    "vault",
    "legacy_id",
    "captured_by",
}


def _read_extras_from_existing(path: Path) -> dict[str, Any]:
    """Read non-serializer-owned frontmatter fields for write-side preservation."""
    if not path.exists():
        return {}
    decoded, _ = _decode_file(path)
    if decoded is None:
        return {}
    split = split_frontmatter(decoded)
    if split is None:
        return {}
    fm_yaml, _ = split
    fm_dict, _ = _parse_frontmatter_yaml(fm_yaml, path)
    if fm_dict is None:
        return {}
    return {k: v for k, v in fm_dict.items() if k not in _SERIALIZER_OWNED_FIELDS}


def write_thought(
    thought: Thought,
    *,
    base_dir: Path,
    preserve_extras_from: Path | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> Path:
    """Atomically write a thought to disk, preserving any unknown extra frontmatter fields.

    Args:
        thought: The thought to write. ``thought.file_path`` is the destination.
        base_dir: The vault thoughts root (used to ensure subdirectories exist).
        preserve_extras_from: Optional path to an existing file whose unknown
            extra frontmatter fields should be preserved on write. If unset,
            extras are read from ``thought.file_path`` itself when it exists.
        extra_fields: Additional frontmatter fields to emit (e.g. consolidation
            provenance on a merged thought). Caller-supplied values win over
            same-named fields preserved from the existing file.

    Returns:
        The absolute path of the written file.
    """
    target = thought.file_path
    if not target.is_absolute():
        target = (base_dir / target).resolve()

    target.parent.mkdir(parents=True, exist_ok=True)

    extras_source = preserve_extras_from if preserve_extras_from is not None else target
    extras = _read_extras_from_existing(extras_source)
    if extra_fields:
        extras.update(extra_fields)
    fm_yaml = _serialize_frontmatter(thought, extras=extras)

    body = thought.content.replace("\r\n", "\n").replace("\r", "\n")
    if body and not body.endswith("\n"):
        body = body + "\n"

    full = f"{_FRONTMATTER_FENCE_NL}{fm_yaml}{_FRONTMATTER_FENCE_NL}{body}"
    atomic_write_text(target, full)
    return target


__all__ = [
    "DriftReason",
    "FrontmatterDrift",
    "read_thought",
    "split_frontmatter",
    "write_thought",
]
