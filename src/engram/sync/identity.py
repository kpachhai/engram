r"""Per-vault identity check defending against cross-vault contamination.

The :class:`IdentityCheck` reads ``<vault>/.engram/identity.local`` (which
is gitignored and therefore machine-local) and compares the configured
``expected_remote_pattern`` against the resolved ``origin`` URL. A
mismatch is **R-H3 cross-vault contamination** - personal thoughts being
pushed to a work remote, or vice versa - and is one of the load-bearing
safety mitigations from Phase 2.

The identity file is YAML and looks like::

    vault_id: example-personal
    expected_remote_pattern: '^git@github.com:owner/.*-personal\.git$'
    user_email: example@example.com
    user_name: Example User

Only ``vault_id`` and ``expected_remote_pattern`` are required. The
optional ``user_email`` / ``user_name`` are surfaced via
:meth:`IdentityCheck.identity` so the coordinator can pass them as
``git -c user.email=... commit`` overrides per R-M14.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML

from engram.errors import ConfigError

IDENTITY_FILE_RELATIVE = Path(".engram") / "identity.local"


class VaultIdentity(BaseModel):
    """Strict-typed payload of ``.engram/identity.local``."""

    model_config = ConfigDict(extra="forbid")

    vault_id: str = Field(min_length=1)
    expected_remote_pattern: str = Field(min_length=1)
    user_email: str | None = None
    user_name: str | None = None


@dataclass(frozen=True, slots=True)
class Match:
    """The configured remote URL matched the expected pattern."""

    identity: VaultIdentity
    matched_url: str


@dataclass(frozen=True, slots=True)
class Mismatch:
    """The configured remote URL did NOT match the expected pattern.

    The coordinator MUST refuse to push when this is the result.
    """

    identity: VaultIdentity
    actual_url: str


@dataclass(frozen=True, slots=True)
class MissingIdentity:
    """No ``.engram/identity.local`` file found."""

    vault_path: Path


IdentityCheck = Match | Mismatch | MissingIdentity


def load_identity(vault_path: Path) -> VaultIdentity | None:
    """Return the parsed :class:`VaultIdentity`, or ``None`` if absent."""
    path = vault_path / IDENTITY_FILE_RELATIVE
    if not path.exists():
        return None
    yaml = YAML(typ="safe", pure=True)
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.load(fh)
    except OSError as exc:
        msg = f"failed to read identity file {path}: {exc}"
        raise ConfigError(msg) from exc
    except Exception as exc:
        msg = f"failed to parse identity YAML {path}: {exc}"
        raise ConfigError(msg) from exc
    if raw is None:
        return None
    if not isinstance(raw, dict):
        msg = f"identity file {path} must be a YAML mapping at top-level"
        raise ConfigError(msg)
    try:
        return VaultIdentity.model_validate(raw)
    except ValidationError as exc:
        msg = f"identity file {path} failed validation: {exc}"
        raise ConfigError(msg) from exc


def check_identity(vault_path: Path, actual_remote_url: str | None) -> IdentityCheck:
    """Run the cross-vault contamination check.

    Returns one of three sentinel dataclasses:

    * :class:`MissingIdentity` - no file present; doctor surfaces a WARN
      (not FAIL) since not every vault has been formally identified yet.
    * :class:`Mismatch` - the file says "personal" and the remote URL
      says "work"; coordinator MUST refuse to push.
    * :class:`Match` - safe to proceed.
    """
    identity = load_identity(vault_path)
    if identity is None:
        return MissingIdentity(vault_path=vault_path)
    if actual_remote_url is None:
        # No remote configured AND a per-vault identity present is suspicious;
        # treat it as a mismatch with empty actual URL so the coordinator can
        # surface a clear message.
        return Mismatch(identity=identity, actual_url="")
    try:
        pattern = re.compile(identity.expected_remote_pattern)
    except re.error as exc:
        msg = (
            f"identity file at {vault_path / IDENTITY_FILE_RELATIVE} has invalid "
            f"expected_remote_pattern regex: {exc}"
        )
        raise ConfigError(msg) from exc
    if pattern.search(actual_remote_url):
        return Match(identity=identity, matched_url=actual_remote_url)
    return Mismatch(identity=identity, actual_url=actual_remote_url)


__all__ = [
    "IDENTITY_FILE_RELATIVE",
    "IdentityCheck",
    "Match",
    "Mismatch",
    "MissingIdentity",
    "VaultIdentity",
    "check_identity",
    "load_identity",
]
