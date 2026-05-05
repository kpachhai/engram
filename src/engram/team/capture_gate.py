"""Phase 4 capture-time gate composition.

Composes the four client-side checks the team-write capture flow runs
BEFORE writing the thought to disk:

1. Storage read-only refusal (already enforced at the storage layer for
   role=read-only; this gate handles the team-write path).
2. ``assert_member_enrolled`` against the team's ``members.yaml``.
3. ``policy.refuse_or_pass`` against the team's ``team-policy.yaml``.
4. Stamp ``thought.captured_by`` with the operator's primary GPG
   fingerprint BEFORE write.

Per pinned invariant 4: client-side is canonical for capture-time
policies (block routing, member enrollment); server-side hook is
canonical for push-time policies (allowlists, attribution integrity,
force-push refusal). The two layers compose: a client-side bypass
(older client, hand-edited markdown) is caught by the server hook.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from engram.errors import VaultReadOnlyError
from engram.team.identity import assert_member_enrolled

if TYPE_CHECKING:
    from engram.models.thought import Thought
    from engram.team.identity import GpgIdentity
    from engram.team.members import MembersList
    from engram.team.policy import TeamVaultPolicy

VaultRole = Literal["primary", "read-only", "team-write"]


def gate_team_capture(
    *,
    thought: Thought,
    role: VaultRole,
    members: MembersList | None,
    policy: TeamVaultPolicy | None,
    gpg_identity: GpgIdentity | None,
) -> Thought:
    """Compose the team-vault capture-time gate.

    Args:
        thought: The thought about to be captured. Mutated to set
            ``captured_by`` when the target is a team-write vault.
        role: The target vault's role (one of "primary", "read-only",
            "team-write").
        members: The team's enrolled-member roster. Required for
            ``team-write``; ignored for other roles.
        policy: The team's vault policy. Required for ``team-write``;
            ignored for other roles.
        gpg_identity: The operator's GPG identity wrapper. Required for
            ``team-write`` (provides the captured_by fingerprint).

    Returns:
        The (possibly captured_by-stamped) thought.

    Raises:
        VaultReadOnlyError: when ``role == "read-only"``.
        TeamMemberNotEnrolled: when ``role == "team-write"`` and the
            local fingerprint is not in members.yaml.
        TeamPolicyViolation: when policy.refuse_or_pass refuses.
        BlockThoughtInTeamVaultDisallowed: defense-in-depth re-assertion
            of pinned invariant 1 (the routing dispatcher catches this
            upstream; the policy gate fires only if a future code path
            bypasses routing).
    """
    if role == "read-only":
        msg = (
            f"vault_read_only: cannot capture into read-only vault "
            f"{thought.vault!r}; mount it as primary or team-write to write"
        )
        raise VaultReadOnlyError(msg)

    if role == "team-write":
        if members is None or policy is None or gpg_identity is None:
            msg = "team-write capture requires non-None members, policy, and gpg_identity"
            raise ValueError(msg)
        # Check 2: member enrollment.
        primary_fp = gpg_identity.primary_fingerprint()
        assert_member_enrolled(members, primary_fp)
        # Check 3: policy refuse-or-pass.
        policy.refuse_or_pass(thought)
        # Check 4: stamp captured_by BEFORE write.
        thought.captured_by = primary_fp

    # role == "primary": no team-gate; capture lands directly.
    return thought


__all__ = ["gate_team_capture"]
