"""Restricted YAML subset parser and fingerprint helpers.

Split out of the pre-receive hook so that parsing untrusted text and deciding
whether to refuse a push are two things rather than one file. This half reads
policy, members and thought frontmatter that arrive from a pusher; it makes no
decisions and touches no git.

PyYAML is deliberately not used: the hook is copied to a bare remote and must be
stdlib-only. The schema it reads is small and fixed, which is what makes a
hand-parser tractable here and dangerous anywhere wider.
"""

from __future__ import annotations

import re

# === Restricted YAML-subset parser ===
# We avoid PyYAML so the hook is stdlib-only. The team policy + members
# YAML is a small fixed schema; we hand-parse it.

_FRONTMATTER_FENCE = "---\n"
_FINGERPRINT_RE = re.compile(r"^[A-F0-9]{40}$")  # vocab-allow: hex char class


def _parse_simple_yaml(text: str) -> dict[str, object]:
    """Hand-parse a restricted YAML subset.

    Supported:
    * top-level scalars: ``key: value`` (value may be quoted)
    * top-level lists: ``key:`` followed by ``  - <item>`` lines
    * top-level booleans: ``true`` / ``false``
    * top-level None / null
    * comments after ``#``
    * one level of dict-list ``- key: value`` items

    NOT supported: nested dicts beyond list-of-dict, multi-line strings,
    anchors, references. The hook's policy/members schema deliberately
    avoids these to keep the parser tractable.
    """
    result: dict[str, object] = {}
    current_list: list[object] | None = None
    current_list_key: str | None = None
    pending_dict_in_list: dict[str, object] | None = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        # List item: starts with optional whitespace + "- "
        stripped = line.lstrip()
        leading_ws = len(line) - len(stripped)
        if stripped.startswith("- "):
            if current_list is None:
                continue
            item = stripped[2:].strip()
            if ":" in item:
                # "- key: value" - dict-list item
                pending_dict_in_list = {}
                k, v = item.split(":", 1)
                pending_dict_in_list[k.strip()] = _coerce_scalar(v.strip())
                current_list.append(pending_dict_in_list)
            else:
                current_list.append(_coerce_scalar(item))
                pending_dict_in_list = None
            continue
        # Continuation of dict-list item (indented "key: value").
        if (
            leading_ws > 0
            and pending_dict_in_list is not None
            and ":" in stripped
            and not stripped.startswith("- ")
        ):
            k, v = stripped.split(":", 1)
            pending_dict_in_list[k.strip()] = _coerce_scalar(v.strip())
            continue
        # Top-level mapping: "key: value" or "key:".
        if ":" in line and leading_ws == 0:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if not val:
                # New list or dict block.
                current_list = []
                current_list_key = key
                result[key] = current_list
                pending_dict_in_list = None
            else:
                result[key] = _coerce_scalar(val)
                current_list = None
                current_list_key = None
                pending_dict_in_list = None

    # If a key opened a list but no items followed, normalize to empty list.
    if current_list_key and current_list is not None and not current_list:
        result[current_list_key] = []
    return result


#: YAML 1.1 boolean spellings. PyYAML (used by the client-side policy model)
#: reads all of these as booleans, so the hook must agree: ``accept_sensitive: no``
#: coerced to the truthy string ``'no'`` disables the sensitive-thought gate on the
#: server while the client believes it is switched off.
_YAML_TRUE = frozenset({"true", "yes", "on", "y"})
_YAML_FALSE = frozenset({"false", "no", "off", "n"})


def _coerce_scalar(value: str) -> object:
    """Coerce a YAML scalar to its Python type (str / int / bool / None / list)."""
    v = value.strip()
    if v in {"null", "~", ""}:
        return None
    if v.lower() in _YAML_TRUE:
        return True
    if v.lower() in _YAML_FALSE:
        return False
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        # Inline list: ``[a, b, c]``.
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_coerce_scalar(item.strip()) for item in inner.split(",")]
    try:
        return int(v)
    except ValueError:
        return v


def _split_frontmatter(text: str) -> tuple[dict[str, object], str] | None:
    r"""Split ``---\nYAML\n---\nbody`` into (parsed_yaml_dict, body)."""
    if not text.startswith(_FRONTMATTER_FENCE):
        return None
    rest = text[len(_FRONTMATTER_FENCE) :]
    end = rest.find("\n" + _FRONTMATTER_FENCE)
    if end == -1:
        if rest.endswith("\n---"):
            return _parse_simple_yaml(rest[:-4]), ""
        return None
    fm_yaml = rest[:end]
    body = rest[end + len("\n" + _FRONTMATTER_FENCE) :]
    return _parse_simple_yaml(fm_yaml), body


def _normalize_fingerprint(fp: str) -> str:
    return fp.upper().replace(" ", "").replace(":", "")


def _is_valid_fingerprint(fp: str) -> bool:
    return bool(_FINGERPRINT_RE.fullmatch(_normalize_fingerprint(fp)))
