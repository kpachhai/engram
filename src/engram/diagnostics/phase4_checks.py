"""Team-vault doctor probe implementations.

Each ``_check_*`` function returns a list of ``DoctorRow`` objects so the
top-level ``engram doctor`` command can fold them into its output. The
team-vault check codes are:

* ``multiple_team_write_vaults_ok`` (INFO)
* ``team_member_not_enrolled`` (FAIL)
* ``team_pending_pushes`` (INFO with queue depth)
* ``team_membership_revoked`` (FAIL on push-auth-failure)
* ``team_policy_violation_quarantined`` (WARN listing orphans)
* ``serve_config_stale`` (WARN)
* ``routing_rule_priority_collision`` (WARN)
* ``team_vault_embedding_mismatch`` (WARN)

These probes are pure-function over a (config, registry, optional
gpg_identity) tuple so the unit tests can exercise each rejection path
without spinning up a real engram serve process.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from engram.diagnostics.check_codes import (
    GIT_BRANCH_DRIFTED,
    MULTIPLE_TEAM_WRITE_VAULTS_OK,
    ROUTING_RULE_PRIORITY_COLLISION,
    SERVE_CONFIG_STALE,
    TEAM_MEMBER_NOT_ENROLLED,
    TEAM_PENDING_PUSHES,
    TEAM_POLICY_VIOLATION_QUARANTINED,
)

if TYPE_CHECKING:
    from engram.config.models import UserConfig
    from engram.diagnostics.doctor import DoctorReport
    from engram.team.identity import GpgIdentity
    from engram.team.members import MembersList

_log = logging.getLogger("engram.diagnostics.phase4_checks")

DoctorStatus = Literal["OK", "INFO", "WARN", "FAIL"]


@dataclass(frozen=True)
class Phase4DoctorRow:
    """One row in the team-vault portion of the doctor report.

    Mirrors the single-vault and multi-vault row shape so the top-level
    doctor command can fold all rows into a uniform output. The class
    name is part of the public API.
    """

    code: str
    status: DoctorStatus
    detail: str


def check_multiple_team_write_vaults(config: UserConfig) -> Phase4DoctorRow:
    """INFO row counting team-write mounts."""
    count = sum(1 for v in config.vaults if v.role == "team-write")
    return Phase4DoctorRow(
        code=MULTIPLE_TEAM_WRITE_VAULTS_OK,
        status="INFO",
        detail=f"{count} team-write vault(s) mounted",
    )


def check_team_member_enrollment(
    *,
    config: UserConfig,
    members_lookup: dict[str, MembersList],
    gpg_identity: GpgIdentity | None,
) -> list[Phase4DoctorRow]:
    """FAIL when the local GPG fingerprint is absent from any team-vault."""
    rows: list[Phase4DoctorRow] = []
    team_vaults = [v for v in config.vaults if v.role == "team-write"]
    if not team_vaults:
        return rows

    if gpg_identity is None:
        return [
            Phase4DoctorRow(
                code=TEAM_MEMBER_NOT_ENROLLED,
                status="FAIL",
                detail="no GPG identity configured but team-write vaults are mounted",
            ),
        ]

    primary_fp = gpg_identity.primary_fingerprint()
    if primary_fp is None:
        rows.append(
            Phase4DoctorRow(
                code=TEAM_MEMBER_NOT_ENROLLED,
                status="FAIL",
                detail="no GPG signing key found on this machine",
            ),
        )
        return rows

    for vault in team_vaults:
        members = members_lookup.get(vault.name)
        if members is None:
            rows.append(
                Phase4DoctorRow(
                    code=TEAM_MEMBER_NOT_ENROLLED,
                    status="FAIL",
                    detail=f"vault {vault.name!r}: no members.yaml available",
                ),
            )
            continue
        if not members.is_enrolled(primary_fp):
            rows.append(
                Phase4DoctorRow(
                    code=TEAM_MEMBER_NOT_ENROLLED,
                    status="FAIL",
                    detail=(
                        f"vault {vault.name!r}: fingerprint {primary_fp[:16]}... "
                        f"not enrolled; ask a steward to add-member"
                    ),
                ),
            )

    return rows


def check_pending_pushes(
    *,
    config: UserConfig,
) -> list[Phase4DoctorRow]:
    """INFO row reporting per-vault push queue depth."""
    from engram.team.push_queue import PersistentPushQueue

    rows: list[Phase4DoctorRow] = []
    for vault in config.vaults:
        if vault.role != "team-write":
            continue
        queue = PersistentPushQueue(vault_path=Path(vault.path))
        pending = queue.iter_pending()
        if not pending:
            continue
        rows.append(
            Phase4DoctorRow(
                code=TEAM_PENDING_PUSHES,
                status="INFO",
                detail=(
                    f"vault {vault.name!r}: {len(pending)} push(es) queued; "
                    f"oldest enqueued at unix-ts {pending[0].enqueued_at}"
                ),
            ),
        )
    return rows


def check_orphan_quarantine(
    *,
    personal_vault_path: Path | None,
) -> list[Phase4DoctorRow]:
    """WARN when orphan tarballs await operator triage."""
    rows: list[Phase4DoctorRow] = []
    if personal_vault_path is None:
        return rows
    orphans_dir = personal_vault_path / ".engram" / "orphans"
    if not orphans_dir.exists():
        return rows
    orphan_files = sorted(orphans_dir.glob("team-vault-orphan-*.tar.gz"))
    if not orphan_files:
        return rows
    rows.append(
        Phase4DoctorRow(
            code=TEAM_POLICY_VIOLATION_QUARANTINED,
            status="WARN",
            detail=(
                f"{len(orphan_files)} orphan tarball(s) at {orphans_dir}; "
                f"run 'engram orphan-recover --to <vault>' or '--discard'"
            ),
        ),
    )
    return rows


def check_serve_config_stale(
    *,
    config_path: Path | None,
    serve_loaded_at: float | None,
) -> list[Phase4DoctorRow]:
    """WARN when serve's config-load time is older than file mtime."""
    if config_path is None or serve_loaded_at is None:
        return []
    if not config_path.exists():
        return []
    try:
        file_mtime = config_path.stat().st_mtime
    except OSError:
        return []
    if file_mtime <= serve_loaded_at:
        return []
    return [
        Phase4DoctorRow(
            code=SERVE_CONFIG_STALE,
            status="WARN",
            detail=(
                f"~/.config/engram/config.yaml mtime ({file_mtime:.0f}) is newer than "
                f"engram serve's load time ({serve_loaded_at:.0f}); restart serve"
            ),
        ),
    ]


def check_routing_rule_priority_collision(config: UserConfig) -> list[Phase4DoctorRow]:
    """WARN when two routing rules tie on prefix without a priority tiebreaker."""
    rows: list[Phase4DoctorRow] = []
    if not config.routing_rules:
        return rows

    counter = Counter(r.prefix for r in config.routing_rules)
    for prefix, count in counter.items():
        if count <= 1:
            continue
        matching = [r for r in config.routing_rules if r.prefix == prefix]
        priorities = [r.priority for r in matching if r.priority is not None]
        # Tiebreaker exists only if at least one rule has a unique priority.
        has_unique_priority = priorities and len(set(priorities)) > 1
        if not has_unique_priority:
            rows.append(
                Phase4DoctorRow(
                    code=ROUTING_RULE_PRIORITY_COLLISION,
                    status="WARN",
                    detail=(
                        f"prefix {prefix!r} matches {count} routing rules "
                        f"with no priority tiebreaker; will refuse with "
                        f"routing_rule_ambiguous at capture time"
                    ),
                ),
            )

    return rows


def check_branch_drift(
    *,
    storages: dict[str, object],
) -> list[Phase4DoctorRow]:
    """WARN when any vault's branch HEAD differs from mount-time HEAD.

    Args:
        storages: Map of vault-name -> VaultStorage (or any object with
            a ``current_branch_drifted()`` method that returns
            ``(drifted, mounted_at, current)``).
    """
    rows: list[Phase4DoctorRow] = []
    for vault_name, storage in storages.items():
        if not hasattr(storage, "current_branch_drifted"):
            continue
        drifted, mounted_at, current = storage.current_branch_drifted()
        if drifted:
            rows.append(
                Phase4DoctorRow(
                    code=GIT_BRANCH_DRIFTED,
                    status="WARN",
                    detail=(
                        f"vault {vault_name!r}: branch was {mounted_at!r} at mount, "
                        f"is now {current!r}; engram's view of disk may be stale"
                    ),
                ),
            )
    return rows


def run_phase4_checks(
    report: DoctorReport,
    user_config: UserConfig,
    *,
    primary_vault_path: Path | None,
    gpg_identity: GpgIdentity | None = None,
) -> None:
    """Fold the team-vault (phase4) rows into the doctor report.

    One-shot doctor scope: ``check_serve_config_stale`` and
    ``check_branch_drift`` need a live serve process's state (config
    load time / mount-time storages) and cannot fire from a fresh
    doctor process, so they are not run here.
    """
    from ruamel.yaml import YAML

    from engram.diagnostics.doctor import CheckStatus
    from engram.team.members import MembersList

    status_map = {
        "OK": CheckStatus.OK,
        "INFO": CheckStatus.OK,
        "WARN": CheckStatus.WARN,
        "FAIL": CheckStatus.FAIL,
    }

    team_vaults = [v for v in user_config.vaults if v.role == "team-write"]
    members_lookup: dict[str, MembersList] = {}
    yaml_safe = YAML(typ="safe", pure=True)
    for vault in team_vaults:
        members_path = Path(vault.path).expanduser() / ".engram" / "members.yaml"
        try:
            members_lookup[vault.name] = MembersList.from_yaml_dict(
                yaml_safe.load(members_path.read_text(encoding="utf-8")) or {}
            )
        except Exception as exc:
            _log.warning("doctor: could not load %s: %s", members_path, exc)

    if gpg_identity is None and team_vaults:
        from engram.team.identity import GpgIdentity as _GpgIdentity

        candidate = _GpgIdentity()
        if candidate.is_gpg_available():
            gpg_identity = candidate

    rows: list[Phase4DoctorRow] = [check_multiple_team_write_vaults(user_config)]
    rows.extend(
        check_team_member_enrollment(
            config=user_config,
            members_lookup=members_lookup,
            gpg_identity=gpg_identity,
        )
    )
    rows.extend(check_pending_pushes(config=user_config))
    rows.extend(check_orphan_quarantine(personal_vault_path=primary_vault_path))
    rows.extend(check_routing_rule_priority_collision(user_config))
    for row in rows:
        report.add(row.code, status_map[row.status], row.detail)


__all__ = [
    "DoctorStatus",
    "Phase4DoctorRow",
    "check_branch_drift",
    "check_multiple_team_write_vaults",
    "check_orphan_quarantine",
    "check_pending_pushes",
    "check_routing_rule_priority_collision",
    "check_serve_config_stale",
    "check_team_member_enrollment",
    "run_phase4_checks",
]
