"""Daemon state file at ``<vault>/.indexes/engram.state.json``.

Holds: pid, started_at, vault_name, vault_path, hostname, config snapshot.
Used by ``engram daemon status`` and to detect cross-machine sync
confusion (the daemon writes the hostname so a stale state file from a
different machine surfaces as a doctor row rather than silently
reattaching).

"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from engram.utils.atomic_write import atomic_write_text


@dataclass(frozen=True)
class DaemonState:
    """Snapshot of a running daemon's identity + config.

    Fields are JSON-serializable so ``write_state`` can dump and
    ``read_state`` can validate via ``DaemonState(**data)``.
    """

    pid: int
    started_at: str  # ISO 8601 UTC
    vault_name: str
    vault_path: str
    hostname: str
    config_snapshot: dict[str, Any]


def write_state(path: Path, state: DaemonState) -> None:
    """Atomically write the state file.

    :func:`engram.utils.atomic_write.atomic_write_text` enforces mode
    ``0o600`` internally; no explicit mode kwarg is required.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(state), separators=(",", ":"))
    atomic_write_text(path, payload)


def read_state(path: Path) -> DaemonState | None:
    """Return the parsed state, or ``None`` if missing / corrupt / schema-drifted.

    Schema drift (an old version on disk that does not match the current
    ``DaemonState`` field set) is treated identically to corruption — the
    caller decides whether to overwrite or surface the row via doctor.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return DaemonState(**data)
    except TypeError:
        # Schema drift (missing or extra fields).
        return None


__all__ = ["DaemonState", "read_state", "write_state"]
