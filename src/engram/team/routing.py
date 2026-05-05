"""Phase 4 per-prefix routing dispatcher.

Determines the target vault for a capture given:

* the captured thought (its portability + first prefix),
* an optional explicit ``vault:`` argument from
  :class:`engram.models.mcp.CaptureInputMetadata`,
* the user's configured routing rules + ``auto_route`` flag,
* the registry of currently mounted vaults (for membership checks +
  team-policy lookup),
* and the team-policy of any candidate target (to enforce
  ``accept_sensitive`` precedence).

Precedence (per Phase 4 plan Step 8):

1. ``portability == "block"`` -> primary (pinned invariant 1).
2. ``portability == "sensitive"`` AND target's ``accept_sensitive=False``
   -> primary (matches invariant 1).
3. Explicit ``vault:`` arg -> that name (pinned invariant 2). Refuses if
   not mounted; refuses if explicit-team-target has incompatible
   ``accept_sensitive`` (operator surprise vs silent fall-through).
4. ``auto_route=True`` AND a routing rule matches the first prefix ->
   that rule's ``target_vault``.
5. Otherwise -> primary.

Multi-prefix tie-break: only the FIRST prefix participates (matches
Phase 3 ``parse_prefix_from_content`` behavior). When multiple rules
match, longest-pattern-match wins; ties broken by user-config
declaration order; remaining ties refuse with ``RoutingRuleAmbiguous``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from engram.errors import (
    RoutingRuleAmbiguous,
    RoutingTargetNotMounted,
    TeamPolicyViolation,
)

if TYPE_CHECKING:
    from engram.config.models import RoutingRule, UserConfig
    from engram.models.thought import Thought
    from engram.multivault.registry import VaultRegistry
    from engram.team.policy import TeamVaultPolicy


# Re-export RoutingRule so callers don't need to import from two places.
@dataclass
class RoutingDecision:
    """Result of :func:`resolve_target_vault`: the target name + the reason.

    ``reason`` is one of: ``"block_portability_to_primary"``,
    ``"sensitive_target_does_not_accept_to_primary"``,
    ``"explicit_arg"``, ``"auto_route_match"``, ``"fallback_primary"``.
    The reason is surfaced in INFO logs so misconfigured users notice
    when an explicit arg overrode a routing rule (per Step 8 SF-10).
    """

    target_vault: str
    reason: str


# Pattern for splitting "[Prefix1][Prefix2] body..." into prefix list.
_PREFIX_RE = re.compile(r"^\[([^\]\[\n]+)\]")


def _first_prefix(content: str) -> str | None:
    """Extract the first ``[Prefix]`` token from ``content`` (or None).

    Matches Phase 3 ``parse_prefix_from_content`` first-match behavior;
    when content begins with ``[Postmortem][Decision] body``, only
    ``Postmortem`` participates in routing.
    """
    match = _PREFIX_RE.match(content.lstrip())
    return match.group(1) if match else None


def _matching_rules_for_prefix(
    prefix: str,
    rules: list[RoutingRule],
) -> list[RoutingRule]:
    """Return rules whose ``prefix`` matches ``prefix`` (case-sensitive)."""
    return [r for r in rules if r.prefix == prefix]


def _resolve_rule_tie_break(rules: list[RoutingRule]) -> RoutingRule:
    """Tie-break multiple matching rules.

    Longest-pattern wins; ties broken by config declaration order
    (already preserved as input order); remaining ties refuse with
    ``RoutingRuleAmbiguous``.
    """
    if len(rules) == 1:
        return rules[0]
    # Longest-pattern-match wins.
    max_len = max(len(r.prefix) for r in rules)
    longest = [r for r in rules if len(r.prefix) == max_len]
    if len(longest) == 1:
        return longest[0]
    # Sort by priority (higher number = higher priority) when set; ties
    # fall through to declaration order (the input order).
    with_priority = [r for r in longest if r.priority is not None]
    if with_priority:
        max_priority = max(r.priority for r in with_priority if r.priority is not None)
        winners = [r for r in with_priority if r.priority == max_priority]
        if len(winners) == 1:
            return winners[0]
    # Ambiguous: refuse loudly.
    msg = (
        f"routing_rule_ambiguous: {len(rules)} rules match the same prefix "
        f"with no deterministic tie-breaker; "
        f"{[(r.prefix, r.target_vault) for r in rules]!r}"
    )
    raise RoutingRuleAmbiguous(msg)


def resolve_target_vault(
    *,
    thought: Thought,
    explicit_vault: str | None,
    user_config: UserConfig,
    registry: VaultRegistry,
    target_policy_lookup: dict[str, TeamVaultPolicy] | None = None,
) -> RoutingDecision:
    """Resolve which vault should receive ``thought``.

    Args:
        thought: The captured thought (carries portability + content prefix).
        explicit_vault: The ``meta.vault`` arg from ``capture_thought``;
            None means "no explicit arg".
        user_config: The user's loaded :class:`UserConfig` (carries
            ``auto_route`` + ``routing_rules``).
        registry: The mounted-vault registry (for membership + role checks).
        target_policy_lookup: Optional map of vault-name -> TeamVaultPolicy
            so the dispatcher can consult ``accept_sensitive``. Pass an
            empty dict (or None) when policies haven't been loaded yet;
            the dispatcher defaults to the safe fall-through-to-primary
            for sensitive thoughts when the target policy isn't known.

    Returns:
        :class:`RoutingDecision` naming the target vault + the reason.

    Raises:
        RoutingTargetNotMounted: explicit arg names an unmounted vault,
            or routing-rule target is unmounted.
        RoutingRuleAmbiguous: multiple rules match with no deterministic
            tie-breaker.
        TeamPolicyViolation: explicit-vault target rejects sensitive when
            ``accept_sensitive=False`` (operator surprise per Step 8
            invariant; silent fall-through would surprise the user).
    """
    primary_name = registry.primary_name()
    policy_lookup = target_policy_lookup or {}

    # Rule 1: block portability -> primary, always.
    if thought.portability == "block":
        return RoutingDecision(target_vault=primary_name, reason="block_portability_to_primary")

    # Rule 3: explicit arg wins (when supplied).
    if explicit_vault is not None:
        if explicit_vault not in registry:
            msg = (
                f"routing_target_not_mounted: explicit vault {explicit_vault!r} "
                f"is not mounted in this engram instance; "
                f"mount it via 'engram team-vault join' or correct the argument"
            )
            raise RoutingTargetNotMounted(msg)
        # If user asked for primary explicitly, honor it (covers the case
        # where a routing rule would have routed elsewhere - explicit
        # always wins).
        if explicit_vault == primary_name:
            return RoutingDecision(target_vault=primary_name, reason="explicit_arg")
        # Sensitive thought to a non-accepting team-vault: refuse (the user
        # explicitly asked; silent fall-through would surprise them).
        if thought.portability == "sensitive":
            policy = policy_lookup.get(explicit_vault)
            if policy is not None and not policy.accept_sensitive:
                msg = (
                    f"sensitive_thought_target_does_not_accept: explicit vault "
                    f"{explicit_vault!r} has accept_sensitive=False; "
                    f"either change the target's policy or capture portable"
                )
                raise TeamPolicyViolation(msg)
        return RoutingDecision(target_vault=explicit_vault, reason="explicit_arg")

    # Rule 2: sensitive + would-be-target-has-accept_sensitive=False
    # for the routing-rule path - this is computed below after we know
    # which rule fires. The early check would require us to evaluate
    # rules before the explicit check, contradicting precedence order.

    # Rule 4: auto-route enabled + a rule matches.
    if user_config.auto_route and user_config.routing_rules:
        first = _first_prefix(thought.content)
        if first is not None:
            matching = _matching_rules_for_prefix(first, user_config.routing_rules)
            if matching:
                rule = _resolve_rule_tie_break(matching)
                target = rule.target_vault
                if target not in registry:
                    msg = (
                        f"routing_target_not_mounted: routing rule "
                        f"({first!r} -> {target!r}) names an unmounted "
                        f"vault; mount it via 'engram team-vault join' "
                        f"or remove the rule"
                    )
                    raise RoutingTargetNotMounted(msg)
                # Apply Rule 2 here: sensitive thought + non-accepting
                # target -> fall through to primary (matches invariant 1).
                if thought.portability == "sensitive":
                    policy = policy_lookup.get(target)
                    if policy is not None and not policy.accept_sensitive:
                        return RoutingDecision(
                            target_vault=primary_name,
                            reason="sensitive_target_does_not_accept_to_primary",
                        )
                return RoutingDecision(target_vault=target, reason="auto_route_match")

    # Rule 5: fall through to primary.
    return RoutingDecision(target_vault=primary_name, reason="fallback_primary")


__all__ = ["RoutingDecision", "resolve_target_vault"]
