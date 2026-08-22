r"""Canonical body fingerprint per ``02-TECHNICAL_DESIGN.md`` Storage Schema.

The fingerprint identifies the THOUGHT (body content), not the FILE - metadata
edits do not invalidate it. Normalization rules:

1. Replace ``\r\n`` and ``\r`` with ``\n`` so files saved across editors with
   different line-ending conventions hash identically.
2. Strip trailing whitespace per line so editor "save with whitespace removed"
   does not invalidate.
3. Strip trailing blank lines so "saved with vs without trailing newline" does
   not invalidate.
4. UTF-8 encode and SHA-256 the resulting bytes; return the hex digest.

The body INCLUDES the leading ``[Prefix]`` line if present (per the spec); only
the markdown frontmatter itself is excluded.
"""

from __future__ import annotations

import hashlib

# SHA-256 of the empty byte string. The canonical fingerprint of an empty body.
EMPTY_FINGERPRINT = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # pii-allow: sha256
)


def normalize_body(body: str) -> bytes:
    """Apply the canonical normalization to a body and return its UTF-8 bytes.

    See module docstring for the full normalization steps.
    """
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).encode("utf-8")


def compute_fingerprint(body: str) -> str:
    """Return the SHA-256 hex digest of ``body`` after canonical normalization."""
    return hashlib.sha256(normalize_body(body)).hexdigest()


__all__ = ["EMPTY_FINGERPRINT", "compute_fingerprint", "normalize_body"]
