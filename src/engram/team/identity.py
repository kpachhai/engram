"""GPG identity wrapper for Phase 4 sender attribution.

Phase 4 uses the operator's GPG signing-key primary fingerprint as the
canonical sender id (per pinned invariant 3). This module wraps the
``gpg --list-secret-keys --with-colons`` subprocess invocation; the
``--with-colons`` machine-readable output is hand-parsed (no PyGPG
dependency) so the engram package stays stdlib-only at this surface.

The colon-format walker resolves subkeys to their primary key so a
``git verify-commit`` output naming a subkey maps back to the primary
fingerprint stored in ``members.yaml``.

Subprocess invocation is fully mocked in tests; no real GPG keyring is
required for the test suite.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

from engram.errors import EngramError, TeamMemberNotEnrolled
from engram.team.members import MembersList, normalize_fingerprint

_log = logging.getLogger("engram.team.identity")


class GpgError(EngramError):
    """GPG subprocess invocation failed.

    Common causes: gpg not installed (clearer remediation than the
    stdlib's ``FileNotFoundError``), no signing key configured, gpg-agent
    unreachable.
    """

    error_code: str = "gpg_error"


@dataclass(frozen=True)
class GpgKey:
    """One GPG primary key + its subkeys.

    The primary fingerprint is the canonical sender id per Phase 4
    pinned invariant 3. Subkey fingerprints map back to the primary at
    verify-commit time.
    """

    primary_fingerprint: str
    subkey_fingerprints: tuple[str, ...]
    user_id: str | None = None


class GpgIdentity:
    """Discover the operator's GPG signing identity.

    Uses ``gpg --list-secret-keys --with-colons``. The ``--with-colons``
    format is documented in ``doc/DETAILS`` of the GPG source. Only
    primary keys (``sec`` records) are considered candidates for sender
    attribution; subkeys are resolved back to their primary at
    verify-commit time.
    """

    def __init__(
        self,
        *,
        gpg_binary: str = "gpg",
        run_command: object = None,
    ) -> None:
        """Construct a GpgIdentity wrapper.

        Args:
            gpg_binary: Name (or path) of the gpg binary. Default ``gpg``.
                Tests pass a path to a fake binary.
            run_command: Optional subprocess.run substitute for testing.
                Must accept ``(cmd: list[str], capture_output: bool,
                text: bool, check: bool, timeout: float | None)`` and
                return an object with ``returncode``, ``stdout``,
                ``stderr`` attributes.
        """
        self._gpg = gpg_binary
        self._run = run_command or subprocess.run

    def is_gpg_available(self) -> bool:
        """Return True iff the gpg binary is on PATH."""
        return shutil.which(self._gpg) is not None

    def list_secret_keys(self) -> list[GpgKey]:
        """Return the operator's secret keys (one entry per primary key)."""
        if not self.is_gpg_available():
            msg = (
                f"gpg binary {self._gpg!r} not found on PATH; install gpg "
                f"(brew install gnupg / apt install gnupg) before running "
                f"engram team-vault commands"
            )
            raise GpgError(msg)
        try:
            result = self._run(  # type: ignore[operator]
                [self._gpg, "--list-secret-keys", "--with-colons"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            msg = f"gpg invocation failed: {exc}"
            raise GpgError(msg) from exc
        if result.returncode != 0:
            msg = (
                f"gpg --list-secret-keys exited {result.returncode}: "
                f"{result.stderr.strip() or '<no stderr>'}"
            )
            raise GpgError(msg)
        return _parse_colon_output(result.stdout)

    def primary_fingerprint(self) -> str | None:
        """Return the operator's primary signing fingerprint, or None.

        When the operator has multiple secret keys, the FIRST one in the
        gpg listing is returned (matches gpg's own default-key selection).
        Operators with multiple keys can set ``user.signingkey`` in their
        git config to be explicit; engram does not currently honor that
        override (the assumption is single-signing-key per machine).
        """
        keys = self.list_secret_keys()
        if not keys:
            return None
        return keys[0].primary_fingerprint

    def primary_for_subkey(self, fingerprint: str) -> str | None:
        """Resolve a subkey fingerprint back to its primary, or None.

        ``git verify-commit`` may surface a subkey fingerprint when the
        commit was signed by an authentication / signing subkey. This
        method walks the keyring's primary->subkeys mapping to recover
        the canonical primary fingerprint for ``members.yaml`` lookup.
        """
        canonical = normalize_fingerprint(fingerprint)
        for key in self.list_secret_keys():
            if key.primary_fingerprint == canonical:
                return canonical
            if canonical in key.subkey_fingerprints:
                return key.primary_fingerprint
        return None


def assert_member_enrolled(
    members: MembersList,
    fingerprint: str | None,
) -> None:
    """Refuse if ``fingerprint`` is not enrolled in ``members``.

    Args:
        members: The team's enrolled-member roster.
        fingerprint: The operator's primary fingerprint (or None when
            no GPG key is reachable).

    Raises:
        TeamMemberNotEnrolled: when ``fingerprint`` is None or not enrolled.
    """
    if fingerprint is None:
        msg = (
            "team_member_not_enrolled: no GPG signing key found on this "
            "machine; run 'engram team-vault enroll-key' first"
        )
        raise TeamMemberNotEnrolled(msg)
    if not members.is_enrolled(fingerprint):
        msg = (
            f"team_member_not_enrolled: fingerprint {fingerprint!r} is not "
            f"in the team's members.yaml; ask a steward to run "
            f"'engram team-vault add-member {fingerprint}' and re-pull"
        )
        raise TeamMemberNotEnrolled(msg)


def _parse_colon_output(text: str) -> list[GpgKey]:
    """Parse ``gpg --with-colons`` machine-readable output.

    Format: each line has 17 colon-separated fields. We care about:

    * ``sec`` records (primary secret key) - field 5 = keyid, but we
      want the full fingerprint from the following ``fpr`` record.
    * ``ssb`` records (secret subkey) - same; full fp from the
      following ``fpr`` record.
    * ``fpr`` records - field 10 carries the 40-hex fingerprint of the
      most-recent ``sec`` / ``ssb`` line.
    * ``uid`` records - field 10 is the user-id.

    Tolerant of missing trailing newline + comment lines (lines that
    aren't 17 colon-fields are ignored).
    """
    keys: list[tuple[str, str | None, list[str]]] = []
    current_primary: str | None = None
    current_uid: str | None = None
    current_subs: list[str] = []
    expecting_primary_fpr = False
    expecting_sub_fpr = False

    def _flush() -> None:
        if current_primary is not None:
            keys.append((current_primary, current_uid, list(current_subs)))

    for line in text.splitlines():
        fields = line.split(":")
        if len(fields) < 11:
            continue
        record_type = fields[0]
        if record_type == "sec":
            _flush()
            current_primary = None
            current_uid = None
            current_subs = []
            expecting_primary_fpr = True
            expecting_sub_fpr = False
        elif record_type == "ssb":
            expecting_primary_fpr = False
            expecting_sub_fpr = True
        elif record_type == "fpr":
            fp = normalize_fingerprint(fields[9])
            if expecting_primary_fpr:
                current_primary = fp
                expecting_primary_fpr = False
            elif expecting_sub_fpr:
                current_subs.append(fp)
                expecting_sub_fpr = False
        elif record_type == "uid" and current_uid is None:
            # Take the first uid; some keys carry multiple.
            current_uid = fields[9].strip() or None

    _flush()
    return [
        GpgKey(
            primary_fingerprint=primary,
            subkey_fingerprints=tuple(subs),
            user_id=uid,
        )
        for primary, uid, subs in keys
    ]


__all__ = ["GpgError", "GpgIdentity", "GpgKey", "assert_member_enrolled"]
