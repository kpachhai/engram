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

Locking: vault mutation guards live at the storage layer; the CLI
additionally refuses to run while a serve/daemon lock is held on either
vault. Refuses if either vault is read-only OR if target's policy
disallows the thought OR if the move would create a chain depth >10.
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
    """Wire ``engram move-thought`` to the Typer app."""
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
        from engram.cli.serve import _load_team_vault_deps
        from engram.config.loader import load_config
        from engram.errors import ConfigError
        from engram.multivault.registry import VaultRegistry
        from engram.storage.facade import VaultStorage
        from engram.utils.lock import serve_lock_metadata

        if "/" not in thought_ref:
            typer.echo(
                "error: thought_ref must be <source-vault>/<thought-id>",
                err=True,
            )
            raise typer.Exit(code=2)
        source_vault, thought_id = thought_ref.split("/", 1)

        try:
            config = load_config()
        except ConfigError as exc:
            typer.secho(f"engram move-thought: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        mounts = {m.name: m for m in config.vaults}
        for alias in (source_vault, to):
            if alias not in mounts:
                typer.secho(
                    f"engram move-thought: vault {alias!r} is not in the "
                    f"per-user vaults list; known: {sorted(mounts)}",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(2)
            vault_path = mounts[alias].path.expanduser().resolve()
            lock_meta = serve_lock_metadata(vault_path)
            if lock_meta is not None:
                typer.secho(
                    f"engram move-thought: vault {alias!r} is held by a "
                    f"serve/daemon process (pid={lock_meta.get('pid', '?')}); "
                    "stop it first (`engram daemon stop`).",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(2)

        registry = VaultRegistry()
        storages: list[VaultStorage] = []
        try:
            for alias in (source_vault, to):
                mount = mounts[alias]
                vault_path = mount.path.expanduser().resolve()
                storage = VaultStorage(
                    thoughts_dir=vault_path / "thoughts",
                    index_db_path=vault_path / ".indexes" / "engram.db",
                    embedding_model_name=config.embedding_model,
                    vault_name=alias,
                )
                storages.append(storage)
                registry.mount(name=alias, storage=storage, role=mount.role)

            from typing import cast

            target_policy: TeamVaultPolicy | None = None
            if mounts[to].role == "team-write":
                target_path = mounts[to].path.expanduser().resolve()
                policy, _members = _load_team_vault_deps(target_path, to)
                target_policy = cast("TeamVaultPolicy | None", policy)

            captured_by_for_tombstone: str | None = None
            if mounts[source_vault].role == "team-write":
                from engram.team.identity import GpgIdentity

                captured_by_for_tombstone = GpgIdentity().primary_fingerprint()
                if captured_by_for_tombstone is None:
                    typer.secho(
                        "engram move-thought: source vault is team-write but no "
                        "GPG primary fingerprint is available; the tombstone "
                        "would be rejected by the pre-receive hook. Aborting.",
                        fg=typer.colors.RED,
                        err=True,
                    )
                    raise typer.Exit(2)

            result = move_thought_cmd(
                registry=registry,
                source_vault=source_vault,
                target_vault=to,
                thought_id=thought_id,
                target_policy=target_policy,
                captured_by_for_tombstone=captured_by_for_tombstone,
            )
        except EngramError as exc:
            typer.secho(f"engram move-thought: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc
        finally:
            for storage in storages:
                storage.close()

        typer.echo(
            f"moved {result.thought_id} from {result.source_vault} to "
            f"{result.target_vault} (chain depth {result.chain_depth}); "
            f"tombstone {result.tombstone_id}. Embeddings for the moved "
            f"thought are pending; run `engram doctor --repair` on the "
            f"target vault to regenerate."
        )


__all__ = [
    "MAX_MOVE_CHAIN_DEPTH",
    "MOVE_CHAIN_WARN_DEPTH",
    "MoveResult",
    "MoveThoughtError",
    "move_thought_cmd",
    "register",
]
