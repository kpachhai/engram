"""``engram serve`` CLI command - launches the FastMCP stdio server.

Lifecycle (steps 1-10 are factored into :func:`_init_serve_runtime` so
the daemon — see ``src/engram/daemon/server.py`` — and ``--no-daemon``
both consume the same init helper):

1. Load resolved configuration.
2. Run :func:`engram.sync.startup_probes.run_startup_probes`. On any FAIL,
   exit 2 with a serialized failure list (refuse to serve).
3. Detect cloud-sync vault paths and warn.
4. Acquire the per-vault advisory lock.
5. If ``sync.auto_pull_on_startup``, run :func:`maybe_startup_pull`.
6. Scan markdown for conflict markers; if found, exit nonzero.
7. Open :class:`VaultStorage`.
8. Build the :class:`SyncCoordinator` and attach it to storage.
9. Construct (lazy) :class:`FastEmbedProvider`.
10. Build the FastMCP server.
11. Run its stdio loop (single-process) OR enter the daemon's UDS accept
    loop in the daemon process.
12. On exit: drain the coordinator queue, release the lock, close storage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from engram.config.loader import load_config
from engram.config.models import EffectiveConfig
from engram.embedding.fastembed import FastEmbedProvider
from engram.errors import ConfigError, LockError
from engram.errors import IndexError as EngramIndexError
from engram.logging import configure_logging
from engram.mcp.server import build_server
from engram.storage.facade import VaultStorage

if TYPE_CHECKING:
    from fastmcp import FastMCP
from engram.sync import startup_probes
from engram.sync.coordinator import CoordinatorConfig, SyncCoordinator
from engram.sync.gitops import conflict_marker_scan
from engram.sync.serve_hooks import maybe_startup_pull
from engram.utils.lock import MigrationLock, VaultLock

_log = logging.getLogger("engram.cli.serve")

# Common consumer cloud-sync directory roots. SQLite + flock semantics on these
# providers are unreliable; WARN on detect.
_CLOUD_SYNC_PATH_HINTS = (
    "Dropbox",
    "iCloud Drive",
    "Library/CloudStorage",
    "OneDrive",
    "Google Drive",
)


class ServeInitError(Exception):
    """Raised by :func:`_init_serve_runtime` when init cannot proceed.

    Carries the operator-facing message in ``args[0]`` and the desired
    process exit code in ``exit_code``. Callers translate to whatever
    output channel they own (``typer.Exit`` for the CLI; readiness-pipe
    write for the daemon spawn dance).
    """

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        """Store the operator-facing message and the desired process exit code."""
        super().__init__(message)
        self.exit_code = exit_code


def _looks_like_cloud_sync_path(path: Path) -> str | None:
    """Return the matching cloud-sync hint if ``path`` lives under one, else None."""
    parts = {p.casefold() for p in path.parts}
    for hint in _CLOUD_SYNC_PATH_HINTS:
        if hint.casefold() in parts:
            return hint
    return None


def _coordinator_config_from(config: EffectiveConfig) -> CoordinatorConfig:
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
        auto_push_on_capture=config.sync.auto_push_on_capture,
        use_no_verify=config.sync.use_no_verify,
        migration_held=lambda: MigrationLock.is_held(config.vault_path),
    )


def _load_team_vault_deps(
    vault_path: Path,
    vault_name: str,
) -> tuple[object | None, object | None]:
    """Load a team-write vault's policy + members from ``.engram/``.

    Returns ``(policy, members)``; either is ``None`` (with a WARN) when
    the file is missing or unparseable - the capture gate then refuses
    team-write captures to that vault instead of silently proceeding
    without policy/enrollment enforcement.
    """
    from ruamel.yaml import YAML

    from engram.team.members import MembersList
    from engram.team.policy import TeamVaultPolicy

    yaml_safe = YAML(typ="safe", pure=True)
    policy: object | None = None
    members: object | None = None

    policy_path = vault_path / ".engram" / "team-policy.yaml"
    try:
        policy = TeamVaultPolicy.model_validate(
            yaml_safe.load(policy_path.read_text(encoding="utf-8")) or {}
        )
    except Exception as exc:
        _log.warning(
            "engram serve: team-write vault %r: could not load %s (%s); "
            "captures to it will be refused",
            vault_name,
            policy_path,
            exc,
        )

    members_path = vault_path / ".engram" / "members.yaml"
    try:
        members = MembersList.from_yaml_dict(
            yaml_safe.load(members_path.read_text(encoding="utf-8")) or {}
        )
    except Exception as exc:
        _log.warning(
            "engram serve: team-write vault %r: could not load %s (%s); "
            "captures to it will be refused",
            vault_name,
            members_path,
            exc,
        )

    return policy, members


def _build_multivault_server_for(
    *,
    config: EffectiveConfig,
    primary_storage: VaultStorage,
    embedder: object,
    primary_coordinator: object | None,
    gpg_identity: object | None = None,
) -> FastMCP[Any]:
    """Build a multi-vault FastMCP server.

    Mounts the primary storage (already opened upstream) into a fresh
    registry, then opens a read-only-mounted storage for every other
    vault listed in ``config.vaults``. Skips entries whose path is
    missing on disk (the operator sees a one-line WARN per skip).

    For every mounted ``team-write`` vault, its ``.engram/team-policy.yaml``
    and ``.engram/members.yaml`` are loaded into the handler deps so the
    capture gate can enforce enrollment + policy and stamp ``captured_by``.
    ``gpg_identity`` defaults to a real :class:`GpgIdentity` when any
    team-write vault is mounted; tests inject a hermetic fake.
    """
    from engram.llm.budget import LLMBudget
    from engram.mcp.llm_tools import HandlerDeps
    from engram.mcp.server import build_multivault_server
    from engram.multivault.registry import VaultRegistry
    from engram.storage.sqlite import set_setting

    registry = VaultRegistry()
    registry.mount(
        name=config.vault_name,
        storage=primary_storage,
        role="primary",
        coordinator=primary_coordinator,
    )

    team_policies: dict[str, object] = {}
    team_members: dict[str, object] = {}

    for mount in config.vaults:
        if mount.name == config.vault_name:
            continue
        vault_path = mount.path.expanduser().resolve()
        if not vault_path.exists():
            _log.warning(
                "engram serve: skipping %r - path %s does not exist",
                mount.name,
                vault_path,
            )
            continue
        try:
            extra = VaultStorage(
                thoughts_dir=vault_path / "thoughts",
                index_db_path=vault_path / ".indexes" / "engram.db",
                embedding_dim=embedder.dimension,  # type: ignore[attr-defined]
                embedding_model_name=config.embedding_model,
                vault_name=mount.name,
            )
            set_setting(extra.conn, "embedding_model_name", config.embedding_model)
            set_setting(extra.conn, "embedding_dim", str(embedder.dimension))  # type: ignore[attr-defined]
            registry.mount(name=mount.name, storage=extra, role=mount.role)
        except Exception as exc:
            hint = ""
            if "primary vault is already mounted" in str(exc):
                vault_config = vault_path / "engram.config.yaml"
                if vault_config.exists():
                    hint = (
                        f" Hint: vault at {vault_path} has its own vault_name in "
                        f"engram.config.yaml that likely differs from {mount.name!r}; "
                        "run 'engram doctor' to detect the mismatch."
                    )
            _log.exception("engram serve: could not mount %r.%s", mount.name, hint)
            continue

        if mount.role == "team-write":
            policy, members = _load_team_vault_deps(vault_path, mount.name)
            if policy is not None:
                team_policies[mount.name] = policy
            if members is not None:
                team_members[mount.name] = members

    if gpg_identity is None and any(
        registry.role_of(name) == "team-write" for name in registry.names()
    ):
        from engram.team.identity import GpgIdentity

        gpg_identity = GpgIdentity()

    budget = LLMBudget.load_or_init(
        state_path=config.index_dir / "llm_usage.json",
        daily_cost_cap_usd=config.llm.daily_cost_cap_usd,
    )
    deps = HandlerDeps(
        registry=registry,
        embedder=embedder,  # type: ignore[arg-type]
        config=config,
        budget=budget,
        team_policies=team_policies,
        team_members=team_members,
        gpg_identity=gpg_identity,
    )
    return build_multivault_server(
        registry,
        embedder,  # type: ignore[arg-type]
        deps,
        default_user=config.default_user,
        server_name="engram",
    )


@dataclass
class ServeRuntime:
    """Resources owned across the serve lifecycle.

    Both the daemon and ``engram serve --no-daemon`` receive this
    dataclass from :func:`_init_serve_runtime`. The serve-side caller
    then either runs ``fastmcp_server.run()`` (single-process stdio
    loop) or hands the server to the daemon's per-connection dispatch
    loop, and invokes :meth:`teardown` on exit.

    The ``embedder`` field is typed ``object`` to match engram's
    duck-typed embedder convention (mirrors ``serve_multivault`` and
    lets tests substitute a fake provider).
    """

    config: EffectiveConfig
    vault_lock: VaultLock
    storage: VaultStorage
    coordinator: SyncCoordinator | None
    embedder: object
    fastmcp_server: FastMCP[Any]

    def teardown(self) -> None:
        """Drain the coordinator (best-effort), close storage, release the lock."""
        if self.coordinator is not None:
            try:
                asyncio.run(self.coordinator.stop())
            except Exception:
                _log.exception("coordinator drain raised on shutdown")
        try:
            self.storage.close()
        except Exception:
            _log.exception("storage close raised on shutdown")
        self.vault_lock.release()


async def _init_serve_runtime(
    *,
    config: EffectiveConfig,
    force: bool,
    skip_probes: bool,
    install_signal_handlers: bool = True,
) -> ServeRuntime:
    """Initialize the serve-side runtime (steps 2-10 of the lifecycle).

    Shared between ``engram serve --no-daemon`` (callers pass
    ``install_signal_handlers=True``) and the daemon (callers pass
    ``install_signal_handlers=False`` so the daemon owns its own
    SIGTERM/SIGINT handler). Caller is responsible for config loading
    and ``configure_logging`` BEFORE invoking this helper.

    Raises :class:`ServeInitError` when init cannot proceed; the caller
    surfaces ``exit_code`` and the operator-facing message through
    whatever channel it owns (typer.Exit, daemon readiness pipe, etc.).
    """
    # Step 2: startup probes BEFORE acquiring lock.
    if not skip_probes and (config.vault_path / ".git").exists():
        probe_report = await startup_probes.run_startup_probes(
            config.sync,
            config.vault_path,
            thoughts_dir=config.thoughts_dir,
        )
        if probe_report.has_failures:
            msg = "engram serve: startup probes failed:\n" + startup_probes.serialize_failures(
                probe_report.failures
            )
            raise ServeInitError(msg)
        for warning in probe_report.warnings:
            _log.warning("startup probe %s: %s", warning.code, warning.message)

    # Step 3: cloud-sync path detection (log-only warning).
    cloud_hint = _looks_like_cloud_sync_path(config.vault_path)
    if cloud_hint is not None:
        _log.warning(
            "vault path %s is under a consumer cloud-sync provider (%s); "
            "SQLite locking semantics on these are unreliable. If you need "
            "multi-machine sync, use git-based sync with a non-synced "
            "vault directory instead.",
            config.vault_path,
            cloud_hint,
        )

    # Step 4: acquire VaultLock.
    vault_lock = VaultLock(
        config.vault_path,
        force=force,
        install_signal_handlers=install_signal_handlers,
    )
    try:
        vault_lock.acquire()
    except LockError as exc:
        raise ServeInitError(f"engram serve: {exc}") from exc

    # Step 5: startup pull (no-op when no remote / disabled).
    if (config.vault_path / ".git").exists():
        try:
            await maybe_startup_pull(config.vault_path, config.sync)
        except Exception:
            _log.exception("startup pull crashed; continuing")

    # Step 6: conflict-marker scan -> degraded mode FAIL.
    if conflict_marker_scan(config.thoughts_dir):
        vault_lock.release()
        msg = (
            "engram serve: conflict markers detected in thoughts/; "
            "resolve them then re-run `engram serve`"
        )
        raise ServeInitError(msg)

    # Steps 7 + 9: open storage + embedder.
    try:
        embedder = FastEmbedProvider(model_name=config.embedding_model)
        storage = VaultStorage(
            thoughts_dir=config.thoughts_dir,
            index_db_path=config.index_dir / "engram.db",
            embedding_dim=embedder.dimension,
            embedding_model_name=config.embedding_model,
            vault_name=config.vault_name,
        )
    except EngramIndexError as exc:
        vault_lock.release()
        raise ServeInitError(f"engram serve: {exc}") from exc

    # Step 8: build + attach the coordinator (only if vault is a git repo).
    coordinator: SyncCoordinator | None = None
    if (config.vault_path / ".git").exists() and not config.sync.disabled:
        coordinator = SyncCoordinator(
            repo_dir=config.vault_path,
            config=_coordinator_config_from(config),
        )
        storage.set_sync_coordinator(coordinator)
        # The coordinator's asyncio task is started lazily on first enqueue.

    # Step 10: build the FastMCP server (single-vault or multivault).
    extra_vaults = [v for v in (config.vaults or []) if v.name != config.vault_name]
    if extra_vaults:
        server = _build_multivault_server_for(
            config=config,
            primary_storage=storage,
            embedder=embedder,
            primary_coordinator=coordinator,
        )
    else:
        server = build_server(
            storage,
            embedder,
            default_user=config.default_user,
            server_name="engram",
        )

    _log.info(
        "engram serve starting: vault=%s default_user=%s model=%s extra_vaults=%d",
        config.vault_name,
        config.default_user,
        config.embedding_model,
        len(extra_vaults),
    )

    return ServeRuntime(
        config=config,
        vault_lock=vault_lock,
        storage=storage,
        coordinator=coordinator,
        embedder=embedder,
        fastmcp_server=server,
    )


def _serve_no_daemon(  # pragma: no cover - exercised by serve-mode smoke
    *,
    config: EffectiveConfig,
    force: bool,
    skip_probes: bool,
) -> None:
    """Single-process stdio serve flow (the ``--no-daemon`` escape hatch).

    Bit-for-bit equivalent to pre-Phase-5 ``engram serve``: acquire
    VaultLock directly, run the FastMCP stdio loop in-process, drain on
    exit. The daemon entrypoint (``engram daemon start``) uses
    :func:`_init_serve_runtime` directly with
    ``install_signal_handlers=False`` rather than this wrapper, because
    it owns its own signal handling.
    """
    try:
        runtime = asyncio.run(
            _init_serve_runtime(
                config=config,
                force=force,
                skip_probes=skip_probes,
                install_signal_handlers=True,
            )
        )
    except ServeInitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(exc.exit_code) from exc

    try:
        runtime.fastmcp_server.run()
    finally:
        runtime.teardown()


def _run_proxy(config: EffectiveConfig) -> int:  # pragma: no cover - exercised by smoke
    """Proxy mode: connect to (or spawn) the per-vault daemon and shuffle bytes.

    Exercised end-to-end by the hermetic CLI smoke (which spawns the
    installed binary in a subprocess); the proxy loop wraps real
    stdin/stdout pipes and forks for the daemon spawn dance, neither
    of which is hermetic in a pytest worker.
    """
    from engram.daemon.client import DaemonClient
    from engram.errors import DaemonNotRunningError

    if not config.daemon.auto_spawn:
        from engram.daemon.client import _try_connect

        async def _probe() -> bool:
            from engram.daemon.socket_paths import resolve_paths as _resolve

            paths = _resolve(config.vault_path)
            conn = await _try_connect(paths.socket)
            if conn is None:
                return False
            _reader, writer = conn
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return True

        if not asyncio.run(_probe()):
            msg = (
                f"no daemon running for vault {config.vault_name!r} and "
                f"daemon.auto_spawn=false; run `engram daemon start` or "
                f"flip the config flag"
            )
            typer.secho(f"engram serve: {msg}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from DaemonNotRunningError(msg)

    client = DaemonClient(vault_path=config.vault_path, daemon_config=config.daemon)
    return asyncio.run(client.run_proxy_loop())


def register(app: typer.Typer) -> None:
    """Attach the ``serve`` subcommand to a typer app."""

    @app.command(name="serve")
    def serve_cmd(
        config_path: Path | None = typer.Option(  # noqa: B008
            None,
            "--config",
            help="Path to a vault's engram.config.yaml; bypasses per-user vaults: list.",
        ),
        vault_name: str | None = typer.Option(
            None,
            "--vault",
            help="Which vault from the per-user vaults: list to target.",
        ),
        log_level: str | None = typer.Option(
            None,
            "--log-level",
            help="Override log level (DEBUG/INFO/WARNING/ERROR).",
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Take over the vault lock even if another process appears to hold it.",
        ),
        skip_probes: bool = typer.Option(
            False,
            "--skip-probes",
            help="Skip startup probes (debugging only).",
        ),
        no_daemon: bool = typer.Option(
            False,
            "--no-daemon",
            help=(
                "Run single-process serve (legacy stdio path). Default "
                "is proxy mode: auto-spawn a per-vault daemon and shuffle "
                "bytes between stdin/stdout and the daemon's UDS."
            ),
        ),
    ) -> None:
        """Start the engram MCP server (proxy mode by default).

        Pass ``--no-daemon`` to run the legacy single-process stdio
        path directly. The default proxy mode spawns a per-vault daemon
        (or attaches to a running one) so N concurrent Claude sessions can
        share the same vault.
        """
        try:
            config = load_config(
                explicit_vault_config=config_path,
                vault_name=vault_name,
                cli_overrides={"log_level": log_level} if log_level else None,
            )
        except ConfigError as exc:
            typer.secho(f"engram serve: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        configure_logging(level=config.log_level, log_format=config.log_format)

        if no_daemon:
            _serve_no_daemon(config=config, force=force, skip_probes=skip_probes)
            return

        exit_code = _run_proxy(config)
        if exit_code != 0:
            raise typer.Exit(exit_code)


__all__ = [
    "ServeInitError",
    "ServeRuntime",
    "_init_serve_runtime",
    "_run_proxy",
    "_serve_no_daemon",
    "register",
]
