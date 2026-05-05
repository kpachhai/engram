"""TeamVaultPolicy - per-team-vault YAML config + capture-time policy gate.

The team policy YAML lives at ``<vault>/.engram/team-policy.yaml`` checked
into the team's git remote (NOT in the per-machine ``.indexes/``). It is
parsed via this Pydantic model with ``extra="forbid"`` so unknown fields
surface as clear validation errors.

The :meth:`TeamVaultPolicy.refuse_or_pass` gate is the capture-time policy
enforcement; the server-side ``pre-receive`` hook is the push-time twin.
Client-side is canonical for capture-time policies; server-side is
canonical for push-time policies. The two layers compose so a client-side
bypass is caught by the server hook.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from engram.errors import (
    BlockThoughtInTeamVaultDisallowed,
    TeamPolicyViolation,
)

if TYPE_CHECKING:
    from engram.models.thought import Thought


class TeamVaultPolicy(BaseModel):
    """Per-team-vault policy: allowlists + sensitive acceptance + stewards.

    Loaded from ``<vault>/.engram/team-policy.yaml`` at startup AND every
    ``engram doctor`` run (Q3 default). Steward-only mutation is enforced
    by the server-side ``pre-receive`` hook (P4-M5/M6).

    Per pinned invariant 1: ``portability=block`` is ALWAYS refused at the
    capture gate regardless of any allowlist.
    """

    model_config = ConfigDict(extra="forbid")

    #: Allowed prefixes. ``None`` means "any" (default-deny does NOT apply
    #: when the field is omitted entirely; an EMPTY list is the explicit
    #: deny-all). Stick to canonical engram prefixes from
    #: :data:`engram.models.frontmatter.CANONICAL_PREFIXES`.
    allowed_prefixes: list[str] | None = None
    #: Allowed source values. ``None`` means "any"; ``[]`` means deny-all.
    allowed_sources: list[str] | None = None
    #: Whether the team accepts ``portability: sensitive`` thoughts. Default
    #: False per pinned invariant 1.
    accept_sensitive: bool = False
    #: The embedding model the whole team must agree on. Cross-vault search
    #: requires same-model embeddings; mounting a team-write vault with a
    #: mismatching local model refuses with ``team_vault_embedding_mismatch``.
    required_embedding_model: str = Field(min_length=1)
    #: Embedding dimension paired with the model. Used by the doctor probe
    #: to detect future model upgrades that change dim.
    required_embedding_dim: int = Field(gt=0)
    #: GPG fingerprints (40 hex, primary key) with disaster-recovery,
    #: policy-mutation, and member-mutation permission. Empty list refuses
    #: setup (every team needs at least one steward).
    stewards: list[str] = Field(default_factory=list)
    #: Minimum engram version for clients that interact with this vault.
    #: Older clients see "upgrade to engram >= X" rather than silent push
    #: refusal (P4-M7).
    min_engram_version: str = "0.4.0"

    def refuse_or_pass(self, thought: Thought) -> None:
        """Refuse the capture if the thought violates this policy.

        Raises:
            BlockThoughtInTeamVaultDisallowed: ``thought.portability ==
                "block"`` (always; pinned invariant 1; defense-in-depth -
                the routing dispatcher catches this upstream but re-asserts
                here).
            TeamPolicyViolation: ``thought.prefix`` not in
                ``allowed_prefixes`` (when explicit allowlist is set);
                ``thought.source`` not in ``allowed_sources`` (when set);
                ``thought.portability == "sensitive"`` and
                ``accept_sensitive`` is False.
        """
        # Defense-in-depth: block portability never lands in a team-write
        # vault. Routing dispatcher catches this upstream (Step 8); the
        # gate also asserts in case of bypass.
        if thought.portability == "block":
            msg = f"thought {thought.id} has portability=block; refused at team-vault gate"
            raise BlockThoughtInTeamVaultDisallowed(msg)

        if self.allowed_prefixes is not None and thought.prefix not in self.allowed_prefixes:
            msg = (
                f"prefix_not_allowed: thought {thought.id} has prefix={thought.prefix!r}; "
                f"team-vault allows only {sorted(self.allowed_prefixes)!r}"
            )
            raise TeamPolicyViolation(msg)

        if self.allowed_sources is not None and thought.source not in self.allowed_sources:
            msg = (
                f"source_not_allowed: thought {thought.id} has source={thought.source!r}; "
                f"team-vault allows only {sorted(self.allowed_sources)!r}"
            )
            raise TeamPolicyViolation(msg)

        if thought.portability == "sensitive" and not self.accept_sensitive:
            msg = (
                f"sensitive_thought_target_does_not_accept: thought {thought.id} is "
                f"portability=sensitive but team-vault has accept_sensitive=False"
            )
            raise TeamPolicyViolation(msg)


__all__ = ["TeamVaultPolicy"]
