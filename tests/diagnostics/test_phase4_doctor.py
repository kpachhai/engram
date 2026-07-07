"""Tests for engram.diagnostics.phase4_checks doctor probes."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from engram.config.models import RoutingRule, UserConfig, VaultMount
from engram.diagnostics.check_codes import (
    MULTIPLE_TEAM_WRITE_VAULTS_OK,
    ROUTING_RULE_PRIORITY_COLLISION,
    SERVE_CONFIG_STALE,
    TEAM_MEMBER_NOT_ENROLLED,
    TEAM_PENDING_PUSHES,
    TEAM_POLICY_VIOLATION_QUARANTINED,
)
from engram.diagnostics.phase4_checks import (
    check_multiple_team_write_vaults,
    check_orphan_quarantine,
    check_pending_pushes,
    check_routing_rule_priority_collision,
    check_serve_config_stale,
    check_team_member_enrollment,
)
from engram.team.members import MemberEntry, MembersList
from engram.team.push_queue import PersistentPushQueue

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint
OTHER_FP = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"  # pii-allow: synthetic test fingerprint


def _config(*, vaults: list[VaultMount] | None = None, **kwargs) -> UserConfig:
    return UserConfig(vaults=vaults or [], **kwargs)


def _team_vault(name: str, path: Path) -> VaultMount:
    return VaultMount(
        name=name,
        path=path,
        role="team-write",
        remote_url=f"git@example:{name}.git",
    )


# === multiple_team_write_vaults_ok ===


def test_multiple_team_write_vaults_zero(tmp_path: Path) -> None:
    config = _config(
        vaults=[VaultMount(name="personal", path=tmp_path / "p", role="primary")],
    )
    row = check_multiple_team_write_vaults(config)
    assert row.code == MULTIPLE_TEAM_WRITE_VAULTS_OK
    assert row.status == "INFO"
    assert "0" in row.detail


def test_multiple_team_write_vaults_two(tmp_path: Path) -> None:
    config = _config(
        vaults=[
            VaultMount(name="personal", path=tmp_path / "p", role="primary"),
            _team_vault("team-x", tmp_path / "x"),
            _team_vault("team-y", tmp_path / "y"),
        ],
    )
    row = check_multiple_team_write_vaults(config)
    assert "2" in row.detail


# === team_member_not_enrolled ===


def test_team_member_enrollment_no_team_vaults(tmp_path: Path) -> None:
    config = _config(
        vaults=[VaultMount(name="personal", path=tmp_path / "p", role="primary")],
    )
    rows = check_team_member_enrollment(
        config=config,
        members_lookup={},
        gpg_identity=None,
    )
    assert rows == []


def test_team_member_enrollment_no_gpg_identity(tmp_path: Path) -> None:
    config = _config(vaults=[_team_vault("team-x", tmp_path / "x")])
    rows = check_team_member_enrollment(
        config=config,
        members_lookup={},
        gpg_identity=None,
    )
    assert len(rows) == 1
    assert rows[0].code == TEAM_MEMBER_NOT_ENROLLED
    assert rows[0].status == "FAIL"


def test_team_member_enrollment_local_key_unenrolled(tmp_path: Path) -> None:
    config = _config(vaults=[_team_vault("team-x", tmp_path / "x")])
    members = MembersList(members=[MemberEntry(fingerprint=OTHER_FP)])
    gpg = MagicMock()
    gpg.primary_fingerprint.return_value = VALID_FP
    rows = check_team_member_enrollment(
        config=config,
        members_lookup={"team-x": members},
        gpg_identity=gpg,
    )
    assert len(rows) == 1
    assert rows[0].code == TEAM_MEMBER_NOT_ENROLLED
    assert "not enrolled" in rows[0].detail


def test_team_member_enrollment_passes_when_enrolled(tmp_path: Path) -> None:
    config = _config(vaults=[_team_vault("team-x", tmp_path / "x")])
    members = MembersList(members=[MemberEntry(fingerprint=VALID_FP)])
    gpg = MagicMock()
    gpg.primary_fingerprint.return_value = VALID_FP
    rows = check_team_member_enrollment(
        config=config,
        members_lookup={"team-x": members},
        gpg_identity=gpg,
    )
    assert rows == []


def test_team_member_enrollment_no_members_yaml(tmp_path: Path) -> None:
    config = _config(vaults=[_team_vault("team-x", tmp_path / "x")])
    gpg = MagicMock()
    gpg.primary_fingerprint.return_value = VALID_FP
    rows = check_team_member_enrollment(
        config=config,
        members_lookup={},
        gpg_identity=gpg,
    )
    assert len(rows) == 1
    assert "no members.yaml" in rows[0].detail


# === team_pending_pushes ===


def test_pending_pushes_no_queue(tmp_path: Path) -> None:
    vault_path = tmp_path / "team-x"
    vault_path.mkdir()
    config = _config(vaults=[_team_vault("team-x", vault_path)])
    rows = check_pending_pushes(config=config)
    assert rows == []


def test_pending_pushes_with_queued(tmp_path: Path) -> None:
    vault_path = tmp_path / "team-x"
    vault_path.mkdir()
    queue = PersistentPushQueue(vault_path=vault_path)
    queue.enqueue(uuid4(), "thoughts/x.md", now=1234567890)
    config = _config(vaults=[_team_vault("team-x", vault_path)])
    rows = check_pending_pushes(config=config)
    assert len(rows) == 1
    assert rows[0].code == TEAM_PENDING_PUSHES
    assert rows[0].status == "INFO"
    assert "1 push(es) queued" in rows[0].detail


# === team_policy_violation_quarantined ===


def test_orphan_quarantine_empty(tmp_path: Path) -> None:
    rows = check_orphan_quarantine(personal_vault_path=tmp_path)
    assert rows == []


def test_orphan_quarantine_with_orphans(tmp_path: Path) -> None:
    orphans_dir = tmp_path / ".engram" / "orphans"
    orphans_dir.mkdir(parents=True)
    (orphans_dir / "team-vault-orphan-abc.tar.gz").write_bytes(b"")
    rows = check_orphan_quarantine(personal_vault_path=tmp_path)
    assert len(rows) == 1
    assert rows[0].code == TEAM_POLICY_VIOLATION_QUARANTINED
    assert rows[0].status == "WARN"


# === serve_config_stale ===


def test_serve_config_stale_when_file_newer(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("vaults: []\n", encoding="utf-8")
    serve_loaded_at = time.time() - 3600  # one hour ago
    rows = check_serve_config_stale(
        config_path=config_file,
        serve_loaded_at=serve_loaded_at,
    )
    assert len(rows) == 1
    assert rows[0].code == SERVE_CONFIG_STALE


def test_serve_config_stale_when_file_older(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("vaults: []\n", encoding="utf-8")
    serve_loaded_at = time.time() + 3600  # in the future (config is older)
    rows = check_serve_config_stale(
        config_path=config_file,
        serve_loaded_at=serve_loaded_at,
    )
    assert rows == []


def test_serve_config_stale_returns_empty_when_no_path() -> None:
    assert check_serve_config_stale(config_path=None, serve_loaded_at=time.time()) == []


# === routing_rule_priority_collision ===


def test_routing_rule_priority_collision_unambiguous(tmp_path: Path) -> None:
    config = _config(
        vaults=[VaultMount(name="personal", path=tmp_path / "p", role="primary")],
        routing_rules=[
            RoutingRule(prefix="Postmortem", target_vault="team-x"),
        ],
    )
    rows = check_routing_rule_priority_collision(config)
    assert rows == []


def test_routing_rule_priority_collision_ambiguous(tmp_path: Path) -> None:
    config = _config(
        vaults=[VaultMount(name="personal", path=tmp_path / "p", role="primary")],
        routing_rules=[
            RoutingRule(prefix="Postmortem", target_vault="team-x"),
            RoutingRule(prefix="Postmortem", target_vault="team-y"),
        ],
    )
    rows = check_routing_rule_priority_collision(config)
    assert len(rows) == 1
    assert rows[0].code == ROUTING_RULE_PRIORITY_COLLISION


def test_routing_rule_priority_collision_resolved_by_priority(tmp_path: Path) -> None:
    config = _config(
        vaults=[VaultMount(name="personal", path=tmp_path / "p", role="primary")],
        routing_rules=[
            RoutingRule(prefix="Postmortem", target_vault="team-x", priority=5),
            RoutingRule(prefix="Postmortem", target_vault="team-y", priority=10),
        ],
    )
    rows = check_routing_rule_priority_collision(config)
    assert rows == []


# === orchestrator: phase4 rows fold into the doctor report ===


def test_run_phase4_checks_folds_rows_into_report(tmp_path: Path) -> None:
    """The team-vault doctor family must be foldable into a DoctorReport.

    Regression: every phase4 check function existed (and was unit-tested)
    but had zero callers in src/, so `engram doctor` on a team-write
    configuration never emitted team_member_not_enrolled or orphan rows.
    """
    from unittest.mock import MagicMock

    from engram.config.models import UserConfig, VaultMount
    from engram.diagnostics.doctor import DoctorReport
    from engram.diagnostics.phase4_checks import run_phase4_checks

    team = tmp_path / "team-x"
    (team / ".engram").mkdir(parents=True)
    (team / ".engram" / "members.yaml").write_text(
        f"members:\n  - fingerprint: {OTHER_FP}\nrevoked: []\n",
        encoding="utf-8",
    )
    user_config = UserConfig(
        vaults=[
            VaultMount(name="primary", path=tmp_path / "primary", role="primary"),
            VaultMount(
                name="team-x",
                path=team,
                role="team-write",
                remote_url="git@example.com:t/x.git",
            ),
        ],
    )
    gpg = MagicMock()
    gpg.primary_fingerprint.return_value = VALID_FP

    report = DoctorReport()
    run_phase4_checks(
        report,
        user_config,
        primary_vault_path=tmp_path / "primary",
        gpg_identity=gpg,
    )

    names = [c.name for c in report.checks]
    assert "team_member_not_enrolled" in names, names
