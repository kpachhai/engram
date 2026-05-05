"""Tests for the team-write role + vault_id derivation.

Covers: ``VaultMount.role`` widened to ``team-write``, ``vault_id``
derived from ``remote_url``, and ``team-write`` without ``remote_url``
refused at config-load.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from engram.config.models import (
    RoutingRule,
    UserConfig,
    VaultMount,
    derive_vault_id,
)
from engram.errors import TeamWriteRequiresRemote


def test_derive_vault_id_is_deterministic() -> None:
    """Same URL produces same id; different URLs produce different ids."""
    assert derive_vault_id("git@example:team-x.git") == derive_vault_id("git@example:team-x.git")
    assert derive_vault_id("git@example:team-x.git") != derive_vault_id("git@example:team-y.git")


def test_derive_vault_id_is_16_hex() -> None:
    """The vault id is the first 16 hex characters of sha256(remote_url)."""
    vid = derive_vault_id("git@example:team-x.git")
    assert len(vid) == 16
    assert all(c in "0123456789abcdef" for c in vid)


def test_vault_mount_role_widened_to_team_write() -> None:
    """team-write is now a valid role."""
    mount = VaultMount(
        name="team-x",
        path=Path("/tmp/team-x"),
        role="team-write",
        remote_url="git@example:team-x.git",
    )
    assert mount.role == "team-write"
    assert mount.vault_id == derive_vault_id("git@example:team-x.git")


def test_vault_mount_team_write_without_remote_url_refuses() -> None:
    """team-write without remote_url refuses at validation time."""
    with pytest.raises((TeamWriteRequiresRemote, ValidationError)):
        VaultMount(
            name="team-x",
            path=Path("/tmp/team-x"),
            role="team-write",
        )


def test_vault_mount_primary_does_not_require_remote_url() -> None:
    """primary role keeps single-vault semantics."""
    mount = VaultMount(
        name="personal",
        path=Path("/tmp/personal"),
        role="primary",
    )
    assert mount.remote_url is None
    assert mount.vault_id is None


def test_vault_mount_primary_with_remote_derives_vault_id() -> None:
    """When remote_url is supplied (e.g. for sync), vault_id derives."""
    mount = VaultMount(
        name="personal",
        path=Path("/tmp/personal"),
        role="primary",
        remote_url="git@example:personal.git",
    )
    assert mount.vault_id == derive_vault_id("git@example:personal.git")


def test_user_config_one_primary_two_team_write_validates() -> None:
    """Happy case: one primary + N team-write."""
    cfg = UserConfig(
        vaults=[
            VaultMount(name="personal", path=Path("/tmp/p"), role="primary"),
            VaultMount(
                name="team-x",
                path=Path("/tmp/x"),
                role="team-write",
                remote_url="git@example:team-x.git",
            ),
            VaultMount(
                name="team-y",
                path=Path("/tmp/y"),
                role="team-write",
                remote_url="git@example:team-y.git",
            ),
        ],
    )
    assert len([v for v in cfg.vaults if v.role == "team-write"]) == 2


def test_user_config_two_primaries_refuses() -> None:
    """The at-most-one-primary invariant still holds."""
    with pytest.raises(ValidationError, match="primary"):
        UserConfig(
            vaults=[
                VaultMount(name="a", path=Path("/tmp/a"), role="primary"),
                VaultMount(name="b", path=Path("/tmp/b"), role="primary"),
            ],
        )


def test_user_config_two_team_write_same_remote_refuses() -> None:
    """Two aliases for the same remote_url refuse at config-load."""
    with pytest.raises(ValidationError, match="vault_id"):
        UserConfig(
            vaults=[
                VaultMount(
                    name="alias-1",
                    path=Path("/tmp/a"),
                    role="team-write",
                    remote_url="git@example:team-x.git",
                ),
                VaultMount(
                    name="alias-2",
                    path=Path("/tmp/b"),
                    role="team-write",
                    remote_url="git@example:team-x.git",
                ),
            ],
        )


def test_user_config_default_auto_route_is_false() -> None:
    """Per Q2 default: auto_route is opt-in."""
    cfg = UserConfig()
    assert cfg.auto_route is False
    assert cfg.routing_rules == []


def test_user_config_routing_rules_round_trip() -> None:
    cfg = UserConfig(
        auto_route=True,
        routing_rules=[
            RoutingRule(prefix="Postmortem", target_vault="team-x"),
            RoutingRule(prefix="Decision", target_vault="team-y", priority=10),
        ],
    )
    assert len(cfg.routing_rules) == 2
    redumped = UserConfig.model_validate(cfg.model_dump())
    assert redumped == cfg
