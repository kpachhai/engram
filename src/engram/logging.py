"""Structured logging configuration for engram.

All log output goes to stderr; stdout is reserved for the MCP JSON-RPC protocol
in :mod:`engram.cli.serve`. Any code path that prints to stdout (FastEmbed
download progress, dependency-internal `print` calls, etc.) is a protocol-corruption
bug; the global stdout-to-stderr redirect in the serve entry point is the
defense-in-depth, but engram's own logger never writes to stdout regardless.

Configuration is layered:

* Explicit kwargs to :func:`configure_logging` win.
* ``ENGRAM_LOG_LEVEL`` and ``ENGRAM_LOG_FORMAT`` env vars apply when kwargs absent.
* Defaults: level ``INFO``, format ``"text"``.

Secret-shaped fields (``api_key``, ``access_token``, ``password``, ``authorization``,
``x-brain-key``, etc.) are redacted by a structlog processor before any
renderer sees them. Redaction matches by key name (case-insensitive substring),
not by value pattern.
"""

from __future__ import annotations

import logging as stdlib_logging
import os
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

_REDACTED = "<redacted>"

# Match keys whose values are likely secrets. Substring + case-insensitive.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|token|secret|password|authorization|brain[_-]?key)",
    re.IGNORECASE,
)


def _redact_secrets(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Replace any value whose key name suggests a secret with ``<redacted>``."""
    for key in list(event_dict.keys()):
        if _SECRET_KEY_RE.search(str(key)):
            event_dict[key] = _REDACTED
    return event_dict


def _resolve_level(level: str | int | None) -> int:
    """Coerce a level argument to the stdlib numeric level."""
    if level is None:
        level = os.environ.get("ENGRAM_LOG_LEVEL", "INFO")
    if isinstance(level, int):
        return level
    return getattr(stdlib_logging, str(level).upper(), stdlib_logging.INFO)


def configure_logging(
    level: str | int | None = None,
    log_format: str | None = None,
) -> None:
    """Configure structlog so engram log output goes to stderr only.

    Safe to call multiple times; each call resets prior configuration. The
    sys.stderr reference is captured at call time, so callers in test
    environments (where pytest replaces ``sys.stderr``) MUST call this
    inside the test function rather than at module import.

    Args:
        level: Log level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``, ``"ERROR"``)
            or numeric level. Defaults to ``ENGRAM_LOG_LEVEL`` env var, then ``INFO``.
        log_format: ``"text"`` (default; human-friendly) or ``"json"`` (one JSON
            object per line). Defaults to ``ENGRAM_LOG_FORMAT`` env var, then ``"text"``.
    """
    numeric_level = _resolve_level(level)
    raw_format = (
        log_format if log_format is not None else os.environ.get("ENGRAM_LOG_FORMAT", "text")
    )
    resolved_format = raw_format.lower()

    if resolved_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a structlog bound logger.

    The returned object exposes ``debug``, ``info``, ``warning``, ``error``,
    ``critical``, ``bind``, and ``unbind`` methods. Its exact static type
    depends on the configured wrapper class, hence the ``Any`` return.
    """
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger"]
