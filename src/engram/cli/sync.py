"""``engram sync`` - explicit sync operations outside the auto loop.

Refuses to run while ``engram serve`` holds the vault lock; instead the
operator either stops the serve loop or relies on its automatic sync.

Subcommand surface:

* ``engram sync`` (default flags) - pull then push
* ``engram sync --pull`` - explicit pull
* ``engram sync --push`` - explicit push of any committed-not-pushed state
* ``engram sync --first-push`` - empty-repo bootstrap; initial commit + ``-u``
* ``engram sync --resume`` - probe ahead/behind; commit pending if any + push
* ``engram sync compact`` (subcommand) - ``git gc --auto`` + reflog policy
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from engram.config.loader import load_config
from engram.config.models import EffectiveConfig
from engram.errors import ConfigError
from engram.sync import gitops
from engram.sync.coordinator import CoordinatorConfig, SyncCoordinator
from engram.sync.gitops import GitErrorClass
from engram.utils.lock import VaultLock, serve_lock_metadata
from engram.utils.run_command import run_git


def _coordinator_config(config: EffectiveConfig) -> CoordinatorConfig:
    return CoordinatorConfig(
        debounce_window_seconds=config.sync.debounce_window_seconds,
        max_deferral_seconds=config.sync.max_deferral_seconds,
        push_retry_count=config.sync.push_retry_count,
        push_retry_backoff_seconds=config.sync.push_retry_backoff_seconds,
        push_timeout_seconds=config.sync.push_timeout_seconds,
        git_remote=config.sync.git_remote,
        git_branch=config.sync.git_branch,
        role=config.sync.role,
        auto_commit_on_capture=config.sync.auto_commit_on_capture,
        auto_push_on_capture=True,  # explicit sync forces push attempt
        use_no_verify=config.sync.use_no_verify,
        signed_pull_required=config.sync.signed_pull_required,
    )


async def _do_pull(vault_path: Path, config: EffectiveConfig) -> int:
    """Run a single pull --rebase; return exit code (0 on OK)."""
    refusal = await gitops.signed_pull_gate(
        vault_path,
        remote=config.sync.git_remote,
        branch=config.sync.git_branch,
        signed_pull_required=config.sync.signed_pull_required,
        timeout=config.sync.push_timeout_seconds,
    )
    if refusal is not None:
        typer.secho(
            f"engram sync --pull: signed-pull gate: {refusal}",
            fg=typer.colors.RED,
            err=True,
        )
        return 2
    result = await gitops.pull_rebase(
        vault_path,
        config.sync.git_remote,
        config.sync.git_branch,
        timeout=config.sync.push_timeout_seconds,
    )
    if result.error_class is GitErrorClass.OK:
        typer.echo("engram sync --pull: ok")
        return 0
    typer.secho(
        f"engram sync --pull: {result.error_class.value}: {result.stderr.strip()}",
        fg=typer.colors.RED,
        err=True,
    )
    return 2


async def _do_push(vault_path: Path, config: EffectiveConfig) -> int:
    coordinator = SyncCoordinator(
        repo_dir=vault_path,
        config=_coordinator_config(config),
    )
    result = await coordinator.explicit_push()
    if result.error_class is GitErrorClass.OK:
        typer.echo("engram sync --push: ok")
        return 0
    if "vault role is read-only" in result.stderr:
        typer.secho(
            "engram sync --push: vault_read_only - role=read-only refuses to push",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return 2
    typer.secho(
        f"engram sync --push: {result.error_class.value}: {result.stderr.strip()}",
        fg=typer.colors.RED,
        err=True,
    )
    return 2


async def _do_first_push(vault_path: Path, config: EffectiveConfig) -> int:
    """Stage everything, commit, push --set-upstream."""
    add_cp = await asyncio.to_thread(run_git, ["add", "."], cwd=vault_path, check=False)
    if add_cp.returncode != 0:
        typer.secho(
            f"engram sync --first-push: git add failed: {add_cp.stderr.strip()}",
            fg=typer.colors.RED,
            err=True,
        )
        return 2

    # Determine commit message based on existing OB-migration commit (Q4).
    log_cp = await asyncio.to_thread(
        run_git,
        ["log", "-1", "--format=%s"],
        cwd=vault_path,
        check=False,
    )
    has_ob = log_cp.returncode == 0 and "open-brain" in log_cp.stdout.lower()
    msg = (
        "engram: pre-sync baseline (post-migration)"
        if has_ob
        else "engram: initial commit (sync baseline)"
    )

    commit_result = await gitops.commit_paths(
        vault_path,
        [],
        message=msg,
        allow_empty=True,
        no_verify=config.sync.use_no_verify,
    )
    if commit_result.sha is None and not commit_result.nothing_to_commit:
        typer.secho(
            "engram sync --first-push: commit failed",
            fg=typer.colors.RED,
            err=True,
        )
        return 2
    push_result = await gitops.push(
        vault_path,
        config.sync.git_remote,
        config.sync.git_branch,
        set_upstream=True,
        timeout=config.sync.push_timeout_seconds,
    )
    if push_result.error_class is GitErrorClass.OK:
        typer.echo("engram sync --first-push: ok")
        return 0
    typer.secho(
        f"engram sync --first-push: {push_result.error_class.value}: {push_result.stderr.strip()}",
        fg=typer.colors.RED,
        err=True,
    )
    return 2


async def _do_resume(vault_path: Path, config: EffectiveConfig) -> int:
    """Probe ahead/behind; commit pending dirty state then push."""
    entries = await gitops.status_porcelain(vault_path)
    if entries:
        relative_paths: list[str] = []
        for entry in entries:
            target = entry.path
            try:
                # Only commit thoughts/ files; everything else is operator concern.
                Path(target).relative_to(Path("thoughts"))
                relative_paths.append(target)
            except ValueError:
                continue
        if relative_paths:
            await gitops.commit_paths(
                vault_path,
                relative_paths,
                message=f"engram: capture batch (N={len(relative_paths)}) [resume]",
                no_verify=config.sync.use_no_verify,
            )
    return await _do_push(vault_path, config)


async def _do_default(vault_path: Path, config: EffectiveConfig) -> int:
    """Pull then push."""
    pull_rc = await _do_pull(vault_path, config)
    if pull_rc != 0:
        return pull_rc
    return await _do_push(vault_path, config)


def _load_or_die(config_path: Path | None, vault_name: str | None) -> EffectiveConfig:
    try:
        return load_config(explicit_vault_config=config_path, vault_name=vault_name)
    except ConfigError as exc:
        typer.secho(f"engram sync: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


def _refuse_if_serve_running(vault_path: Path) -> None:
    meta = serve_lock_metadata(vault_path)
    if meta is None:
        return
    lock_path = vault_path / ".indexes" / "engram.lock"
    typer.secho(
        (
            f"engram sync: vault lock at {lock_path} is held "
            f"(pid={meta.get('pid', '?')}); "
            "stop the serve loop OR rely on its automatic sync"
        ),
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(2)


def register(app: typer.Typer) -> None:
    """Attach the ``sync`` command + ``sync compact`` subcommand."""
    sync_app = typer.Typer(
        name="sync",
        help="Explicit sync operations (pull, push, first-push, resume).",
        no_args_is_help=False,
    )

    @sync_app.callback(invoke_without_command=True)
    def sync_root(
        ctx: typer.Context,
        config_path: Path | None = typer.Option(  # noqa: B008
            None, "--config", help="Path to a vault's engram.config.yaml."
        ),
        vault_name: str | None = typer.Option(None, "--vault", help="Which vault to sync."),
        push: bool = typer.Option(False, "--push", help="Explicit push only."),
        pull: bool = typer.Option(False, "--pull", help="Explicit pull only."),
        first_push: bool = typer.Option(
            False, "--first-push", help="Bootstrap a non-pushed vault: stage + commit + push -u."
        ),
        resume: bool = typer.Option(
            False,
            "--resume",
            help="Commit any pending dirty state and push (recovery from committed_not_pushed).",
        ),
    ) -> None:
        """Run an explicit sync (default: pull then push)."""
        if ctx.invoked_subcommand is not None:
            return  # `engram sync compact` and friends route below
        config = _load_or_die(config_path, vault_name)
        _refuse_if_serve_running(config.vault_path)

        flags_set = sum([push, pull, first_push, resume])
        if flags_set > 1:
            typer.secho(
                "engram sync: --push, --pull, --first-push, --resume are mutually exclusive",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        async def _runner() -> int:
            if first_push:
                return await _do_first_push(config.vault_path, config)
            if pull:
                return await _do_pull(config.vault_path, config)
            if push:
                return await _do_push(config.vault_path, config)
            if resume:
                return await _do_resume(config.vault_path, config)
            return await _do_default(config.vault_path, config)

        rc = asyncio.run(_runner())
        if rc != 0:
            raise typer.Exit(rc)

    @sync_app.command(name="compact")
    def sync_compact(
        config_path: Path | None = typer.Option(  # noqa: B008
            None, "--config", help="Path to a vault's engram.config.yaml."
        ),
        vault_name: str | None = typer.Option(None, "--vault", help="Which vault to compact."),
    ) -> None:
        """Run quarterly maintenance: ``git gc --auto`` + reflog expire policy."""
        config = _load_or_die(config_path, vault_name)
        _refuse_if_serve_running(config.vault_path)
        # Acquire VaultLock so a stray serve loop cannot race us.
        try:
            with VaultLock(config.vault_path, force=False):
                cp_set = run_git(
                    ["config", "gc.reflogExpire", "30.days.ago"],
                    cwd=config.vault_path,
                    check=False,
                )
                if cp_set.returncode != 0:
                    typer.secho(
                        f"engram sync compact: failed to set gc.reflogExpire: {cp_set.stderr}",
                        fg=typer.colors.YELLOW,
                        err=True,
                    )
                cp_gc = run_git(["gc", "--auto"], cwd=config.vault_path, check=False)
                if cp_gc.returncode != 0:
                    typer.secho(
                        f"engram sync compact: gc --auto exit {cp_gc.returncode}",
                        fg=typer.colors.YELLOW,
                        err=True,
                    )
                    raise typer.Exit(2)
        except Exception as exc:
            typer.secho(f"engram sync compact: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc
        typer.echo("engram sync compact: ok")

    app.add_typer(sync_app)


__all__ = ["register"]
