"""LLM response citation post-validator.

* Parse the LLM response for thought-id-shaped substrings (UUID-v7
  regex).
* Cross-reference against the actually-retrieved set.
* Strip hallucinated citations + replace with ``[citation removed]``.
* Emit a WARN log entry per stripped citation.

Rationale: the user trusted the model with the retrieved set; the model
citing thoughts the retrieval did NOT surface is unverifiable and risks
disclosure of information from outside the user's filter. Conservative
behavior: strip.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

_log = logging.getLogger("engram.llm.citations")

#: Match a UUID (any version, including v7); citations carry the same
#: shape as engram's thought ids.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

_REPLACEMENT_TOKEN = "[citation removed]"  # noqa: S105 - user-facing replacement token, not a credential


@dataclass(slots=True)
class CitationValidationResult:
    """Output of :func:`validate_citations`.

    ``stripped_ids`` is informational; the calling MCP tool surfaces
    the count to the user (and the WARN log entry per id).
    """

    text: str
    stripped_ids: list[str]
    valid_ids: list[str]


def validate_citations(
    *,
    response_text: str,
    retrieved_ids: Iterable[str],
) -> CitationValidationResult:
    """Strip hallucinated thought-id citations from ``response_text``.

    A citation is "valid" if its UUID exactly appears in
    ``retrieved_ids``. Anything else gets replaced with
    :data:`_REPLACEMENT_TOKEN` and logged at WARN level.

    The matcher is exact-string-match against the retrieved set. UUID
    case is normalized to lowercase before comparison so callers don't
    need to pre-normalize.
    """
    retrieved_set = {rid.lower() for rid in retrieved_ids}
    stripped: list[str] = []
    valid: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        norm = candidate.lower()
        if norm in retrieved_set:
            valid.append(norm)
            return candidate
        stripped.append(norm)
        _log.warning(
            "validate_citations: stripping hallucinated citation %s (not in retrieved top-k)",
            candidate,
        )
        return _REPLACEMENT_TOKEN

    cleaned = _UUID_RE.sub(_replace, response_text)
    return CitationValidationResult(text=cleaned, stripped_ids=stripped, valid_ids=valid)
