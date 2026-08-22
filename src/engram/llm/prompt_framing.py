"""Delimiter framing for untrusted text handed to an LLM.

Both LLM call paths - the MCP ``summarize``/``synthesize`` handlers and the
consolidation judge/distiller - wrap stored thought bodies in a delimiter block
and instruct the model to treat everything inside it as data. That posture only
holds while the content cannot forge the frame: a body carrying ``</thought>``
closes its own block, and everything after it reads as prompt rather than data.

Consolidation is the path with a write behind it - under ``engram consolidate
--apply`` the distilled output becomes a real vault thought - so the escaping
lives here and both callers use it rather than each defending its own frame.

This is a ratchet, not a guarantee; indirect prompt injection is unsolved at the
model layer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping


def escape_attribute(value: str) -> str:
    """Escape a value interpolated into a delimiter attribute.

    ``source`` travels with imported bundles, so attribute values are
    attacker-influenced too and must not be able to close the quoted attribute
    and start a new tag.
    """
    return (
        value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


def neutralize_delimiters(text: str, *, tag: str) -> str:
    """Defuse opening and closing ``<tag>`` delimiters inside untrusted text.

    Only the delimiter sequence is escaped, so ordinary prose containing ``<``
    or ``>`` (``if a < b``) still reads naturally to the model.
    """
    pattern = rf"<(/?)\s*{re.escape(tag)}\b"
    return re.sub(pattern, lambda m: f"&lt;{m.group(1)}{tag}", text, flags=re.IGNORECASE)


def frame_block(*, tag: str, body: str, attributes: Mapping[str, str] | None = None) -> str:
    """Render ``body`` as a delimited block, escaping both body and attributes.

    Attributes are emitted in mapping order.
    """
    rendered = "".join(
        f' {name}="{escape_attribute(value)}"' for name, value in (attributes or {}).items()
    )
    return f"<{tag}{rendered}>\n{neutralize_delimiters(body, tag=tag)}\n</{tag}>"


__all__ = ["escape_attribute", "frame_block", "neutralize_delimiters"]
