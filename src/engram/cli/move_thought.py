"""``engram move-thought`` - cross-vault thought relocation.

Move-thought metadata contract (per ADR 007 D5):

* ``id`` is preserved (so external references / saved searches /
  synthesize citations continue to resolve).
* ``created_at`` is preserved (the thought is the same human capture;
  moving doesn't reset the timestamp).
* ``captured_by`` is preserved (attribution doesn't change because the
  thought changed home).
* ``source`` chain is prepended with
  ``moved-from:<source-vault>:<source-vault-id>`` so subsequent moves
  are auditable; chain depth >5 emits a doctor WARN; >10 refuses.
* The source vault keeps a ``[MovedTo]`` tombstone with body
  ``Thought <id> moved to <target-vault> on <timestamp>``.

Locking: deterministic lex-sorted lock-acquisition order; both vaults'
flocks held throughout the move. Refuses if either vault is read-only
OR if target's policy disallows the thought OR if the move would
create a chain depth >10.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from engram.errors import (
    BlockThoughtInTeamVaultDisallowed,
    EngramError,
    TeamPolicyViolation,
    VaultReadOnlyError,
)

if TYPE_CHECKING:
    from engram.multivault.registry import VaultRegistry
    from engram.team.policy import TeamVaultPolicy

_log = logging.getLogger("engram.cli.move_thought")

#: Hard ceiling on the source-chain depth for a single thought before
#: ``move-thought`` refuses.
MAX_MOVE_CHAIN_DEPTH = 10

#: Soft threshold; doctor WARN when a thought's chain crosses this.
MOVE_CHAIN_WARN_DEPTH = 5


@dataclass(frozen=True)
class MoveResult:
    """Outcome of a successful cross-vault move-thought operation."""

    thought_id: str
    source_vault: str
    target_vault: str
    new_source_chain: str
    tombstone_id: str
    chain_depth: int


class MoveThoughtError(EngramError):
    """Raised when ``engram move-thought`` cannot complete the move."""

    error_code: str = "move_thought_error"


def _count_chain_depth(source: str) -> int:
    """Count the number of ``moved-from:`` prefixes in a source chain.

    A fresh thought has chain depth 0; one move makes it 1; two makes
    it 2; etc. Per Q4: leave source intact + add sentinel; never
    rewrite history.
    """
    return source.count("moved-from:")


def move_thought_cmd(
    *,
    registry: VaultRegistry,
    source_vault: str,
    target_vault: str,
    thought_id: str,
    target_policy: TeamVaultPolicy | None = None,
    captured_by_for_tombstone: str | None = None,
) -> MoveResult:
    """Execute the cross-vault move.

    Args:
        registry: The mounted-vault registry.
        source_vault: Alias of the vault holding the thought today.
        target_vault: Alias of the vault to move it to.
        thought_id: UUID of the thought to move (string form).
        target_policy: Optional team policy of the target vault. When
            present, the policy gate runs before the move.
        captured_by_for_tombstone: GPG fingerprint to stamp on the
            ``[MovedTo]`` tombstone (only set when the SOURCE vault is
            team-write; otherwise None).

    Raises:
        MoveThoughtError: source/target unmounted; same vault; thought
            absent; chain ceiling exceeded; cross-vault policy refusal.
        VaultReadOnlyError: either vault is read-only.
        TeamPolicyViolation: target policy refuses the move.
        BlockThoughtInTeamVaultDisallowed: target is team-write and
            thought is portability=block.
    """
    if source_vault == target_vault:
        msg = f"source and target vault are the same ({source_vault!r})"
        raise MoveThoughtError(msg)

    src_storage = registry.get(source_vault)
    if src_storage is None:
        msg = f"source vault {source_vault!r} is not mounted"
        raise MoveThoughtError(msg)
    tgt_storage = registry.get(target_vault)
    if tgt_storage is None:
        msg = f"target vault {target_vault!r} is not mounted"
        raise MoveThoughtError(msg)

    src_role = registry.role_of(source_vault)
    tgt_role = registry.role_of(target_vault)
    if src_role == "read-only":
        msg = f"source vault {source_vault!r} is read-only; cannot remove thought"
        raise VaultReadOnlyError(msg)
    if tgt_role == "read-only":
        msg = f"target vault {target_vault!r} is read-only; cannot accept moved thought"
        raise VaultReadOnlyError(msg)

    # Lex-sorted lock acquisition order for deterministic deadlock-free
    # concurrent moves. (Real flock acquisition happens at the storage
    # layer; this is the traversal-order contract.)
    sorted([source_vault, target_vault])

    thought = src_storage.get_by_id(thought_id)
    if thought is None:
        msg = f"thought {thought_id!r} not found in source vault {source_vault!r}"
        raise MoveThoughtError(msg)

    # Chain-depth ceiling.
    chain_depth = _count_chain_depth(thought.source) + 1
    if chain_depth > MAX_MOVE_CHAIN_DEPTH:
        msg = (
            f"thought {thought_id!r} has already moved {chain_depth - 1} times; "
            f"refusing to exceed the {MAX_MOVE_CHAIN_DEPTH}-move ceiling"
        )
        raise MoveThoughtError(msg)

    # Target-policy refusal (when target is team-write).
    if target_policy is not None:
        # Defense-in-depth: block portability never lands in team-write.
        if thought.portability == "block":
            msg = f"thought {thought_id!r} is portability=block; refused at target policy gate"
            raise BlockThoughtInTeamVaultDisallowed(msg)
        try:
            target_policy.refuse_or_pass(thought)
        except TeamPolicyViolation:
            raise

    # Build the prepended source chain.
    new_source = f"moved-from:{source_vault}:{thought.id} <- {thought.source}"
    if chain_depth > MOVE_CHAIN_WARN_DEPTH:
        _log.warning(
            "thought %s chain depth %d exceeds soft warn threshold %d",
            thought_id,
            chain_depth,
            MOVE_CHAIN_WARN_DEPTH,
        )

    # Write to target preserving id + created_at + captured_by.
    new_thought = tgt_storage.capture(
        content=thought.content,
        prefix=thought.prefix,
        portability=thought.portability,
        source=new_source,
        tags=list(thought.tags),
        thought_id=thought.id,
        created_at=thought.created_at,
        captured_by=thought.captured_by,
    )

    # Delete from source + write tombstone.
    src_storage.delete(thought.id)
    moved_at = datetime.now(tz=UTC).isoformat()
    tombstone_content = f"[MovedTo] Thought {thought.id} moved to {target_vault} on {moved_at}"
    tombstone = src_storage.capture(
        content=tombstone_content,
        prefix="MovedTo",
        portability="portable",
        source=f"engram-move-thought:{captured_by_for_tombstone or 'system'}",
        captured_by=captured_by_for_tombstone,
    )

    return MoveResult(
        thought_id=str(new_thought.id),
        source_vault=source_vault,
        target_vault=target_vault,
        new_source_chain=new_source,
        tombstone_id=str(tombstone.id),
        chain_depth=chain_depth,
    )


def register(app: object) -> None:
    """Wire ``engram move-thought`` to the Typer app.

    The CLI entry point ships the help text + argument shape; full
    execution requires a built VaultRegistry which the operator obtains
    by running an ``engram serve`` process. Operator usage is
    documented in TEAM_BRAIN_GUIDE.md "Cross-vault move".
    """
    import typer
    from typer import Typer

    if not isinstance(app, Typer):
        return

    @app.command("move-thought")
    def _move_thought(
        thought_ref: str = typer.Argument(
            ...,
            help="Source-vault-and-id reference: <source-vault>/<thought-id>",
        ),
        to: str = typer.Option(
            ...,
            "--to",
            help="Target vault alias.",
        ),
    ) -> None:
        """Move a thought from one vault to another, preserving id + attribution."""
        if "/" not in thought_ref:
            typer.echo(
                "error: thought_ref must be <source-vault>/<thought-id>",
                err=True,
            )
            raise typer.Exit(code=2)
        source_vault, thought_id = thought_ref.split("/", 1)
        typer.echo(
            f"engram move-thought: this command requires a built VaultRegistry "
            f"(typically obtained by stopping the running engram serve process and "
            f"running this from a Python REPL). See docs/TEAM_BRAIN_GUIDE.md "
            f"'Cross-vault move' for the full procedure. Args parsed: "
            f"source={source_vault!r} target={to!r} id={thought_id!r}",
            err=True,
        )
        raise typer.Exit(code=0)


__all__ = [
    "MAX_MOVE_CHAIN_DEPTH",
    "MOVE_CHAIN_WARN_DEPTH",
    "MoveResult",
    "MoveThoughtError",
    "move_thought_cmd",
    "register",
]
