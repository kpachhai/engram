"""``engram daemon`` subcommand group.

Subcommands:

- ``start [--detach] [--vault N | --config P] [--force] [--skip-probes]``
  Start the daemon. ``--detach`` double-forks so the caller returns
  immediately; without ``--detach`` the process stays in the foreground
  (useful for debugging and for the proxy spawn dance, which uses
  ``--readiness-fd`` to handshake over a pipe).
- ``stop [--force]`` Send SIGTERM (or SIGKILL with ``--force``) and wait
  for graceful drain bounded by ``daemon.coordinator_flush_seconds + 10``.
- ``status [--json] [--all]`` Read ``engram.state.json`` and emit a
  human-readable or machine-readable status. Not-running exits 0 with
  the structured "not running" shape so consumers can branch on
  ``daemon.running``.
- ``logs [--tail N] [--follow]`` Tail the daemon's log file with
  inode-reopen logic for ``--follow``.

See ``docs/DAEMON_MODE.md`` for the operator guide.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from engram.cli.serve import _init_serve_runtime
from engram.config.loader import load_config
from engram.config.models import EffectiveConfig
from engram.daemon.log_rotation import configure_log_rotation
from engram.daemon.server import DaemonServer
from engram.daemon.socket_paths import resolve_paths
from engram.daemon.spawn import double_fork_detach
from engram.daemon.state import read_state
from engram.errors import ConfigError
from engram.logging import configure_logging

_log = logging.getLogger("engram.cli.daemon")

app = typer.Typer(
    name="daemon",
    help="Daemon-mode lifecycle commands.",
    no_args_is_help=True,
)


# ----- helpers ------------------------------------------------------


def _resolve_config(
    config_path: Path | None,
    vault_name: str | None,
    vault_path_arg: Path | None,
) -> EffectiveConfig:
    """Resolve config from any of the three operator-facing options.

    Priority: ``--vault-path`` (proxy-spawn path) > ``--config`` > ``--vault``.
    """
    if vault_path_arg is not None:
        explicit = vault_path_arg.expanduser().resolve() / "engram.config.yaml"
        if not explicit.exists():
            msg = (
                f"engram daemon: --vault-path {vault_path_arg} has no "
                f"engram.config.yaml; run `engram init {vault_path_arg.name}` first"
            )
            typer.secho(msg, fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        return load_config(explicit_vault_config=explicit)
    return load_config(explicit_vault_config=config_path, vault_name=vault_name)


def _attach_daemon_log_handler(
    config: EffectiveConfig,
) -> None:  # pragma: no cover - mutates global logging
    """Replace stderr handlers with the rotating-file handler for the daemon log."""
    paths = resolve_paths(config.vault_path)
    handler = configure_log_rotation(
        paths.log_file,
        max_size_mb=config.daemon.log_max_size_mb,
        retention_days=config.daemon.log_retention_days,
        level=config.daemon.log_level,
    )
    root = logging.getLogger()
    # Remove pre-existing stream handlers so daemon stdout/stderr stay clean.
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.handlers.RotatingFileHandler
        ):
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, config.daemon.log_level.upper(), logging.INFO))
    # Detach from the proxy's process group so signals sent to the proxy
    # (e.g., SIGTERM when the MCP client exits) don't reach the daemon.
    # Suppressed: setsid() fails if this process is already a session leader,
    # which happens when --detach was used (double_fork_detach already called it).
    with contextlib.suppress(OSError):
        os.setsid()
    # Redirect raw stdout/stderr to /dev/null so the inherited proxy pipe
    # cannot trigger a BrokenPipeError (and rich's SystemExit) when the
    # proxy process exits. This is safe: all daemon output goes to the
    # rotating log file; nothing useful lives on fd 1/2 past this point.
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
    finally:
        os.close(devnull_fd)


def _pid_alive(pid: int) -> bool:
    """Return ``True`` iff the given PID exists (or we can't tell)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but signaling it is refused — still "alive" for us.
        return True
    return True


def _write_readiness_error(
    readiness_fd: int | None, message: str
) -> None:  # pragma: no cover - real pipe required
    r"""Write ``error: <msg>\n`` to the proxy's readiness pipe (best-effort)."""
    if readiness_fd is None:
        return
    payload = f"error: {message}\n".encode()
    with contextlib.suppress(OSError):
        os.write(readiness_fd, payload)
    with contextlib.suppress(OSError):
        os.close(readiness_fd)


async def _run_daemon_serve_forever(  # pragma: no cover - exercised by hermetic CLI smoke
    config: EffectiveConfig,
    *,
    force: bool,
    skip_probes: bool,
    readiness_fd: int | None,
) -> None:
    """Build the runtime + run the daemon's accept loop."""
    from engram.cli.serve import ServeInitError

    try:
        runtime = await _init_serve_runtime(
            config=config,
            force=force,
            skip_probes=skip_probes,
            install_signal_handlers=False,
        )
    except ServeInitError as exc:
        _log.error("daemon init failed: %s", exc)
        _write_readiness_error(readiness_fd, str(exc))
        sys.exit(exc.exit_code)

    server = DaemonServer(
        runtime=runtime,
        daemon_config=config.daemon,
        readiness_fd=readiness_fd,
    )
    await server.serve_forever()


# ----- subcommands -------------------------------------------------


@app.command()
def start(
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
    vault_path_arg: Path | None = typer.Option(  # noqa: B008
        None,
        "--vault-path",
        help=(
            "Vault directory containing engram.config.yaml; used by the "
            "proxy spawn dance. Equivalent to --config <path>/engram.config.yaml."
        ),
    ),
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Double-fork into the background; the caller returns immediately.",
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
    readiness_fd: int | None = typer.Option(
        None,
        "--readiness-fd",
        help=(
            "[internal] File descriptor to which the daemon writes "
            "'ready\\n' (or 'error: <msg>\\n') after bind. Used by the "
            "proxy spawn dance."
        ),
    ),
) -> None:  # pragma: no cover - exercised by hermetic CLI smoke
    """Start the engram daemon for one vault.

    Coverage note: this command forks (``--detach``) and opens real
    files via the rotating-log handler; the end-to-end behavior is
    exercised by the hermetic CLI smoke that spawns the binary in a
    subprocess.
    """
    try:
        config = _resolve_config(config_path, vault_name, vault_path_arg)
    except ConfigError as exc:
        _write_readiness_error(readiness_fd, str(exc))
        typer.secho(f"engram daemon start: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    # UX hint: when run interactively without --detach AND not via the
    # proxy spawn dance (which always passes --readiness-fd), the daemon
    # process literally IS the foreground shell process - it does not
    # fork. The terminal blocks until Ctrl-C. Surfacing this loudly
    # avoids the "engram daemon start is hanging" support surface.
    if not detach and readiness_fd is None:
        typer.secho(
            (
                "engram daemon start: running in foreground "
                "(Ctrl-C to stop, or rerun with --detach to background)."
            ),
            fg=typer.colors.YELLOW,
            err=True,
        )

    if detach:
        double_fork_detach()

    configure_logging(level=config.log_level, log_format=config.log_format)
    _attach_daemon_log_handler(config)
    _log.info(
        "engram daemon start: vault=%s pid=%s detach=%s",
        config.vault_name,
        os.getpid(),
        detach,
    )

    try:
        asyncio.run(
            _run_daemon_serve_forever(
                config,
                force=force,
                skip_probes=skip_probes,
                readiness_fd=readiness_fd,
            )
        )
    except KeyboardInterrupt:
        _log.info("daemon interrupted; exiting")


@app.command()
def stop(
    config_path: Path | None = typer.Option(  # noqa: B008
        None, "--config", help="Path to a vault's engram.config.yaml."
    ),
    vault_name: str | None = typer.Option(
        None, "--vault", help="Vault name from the per-user vaults list."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Send SIGKILL after the graceful timeout instead of failing.",
    ),
    timeout: float = typer.Option(
        60.0,
        "--timeout",
        help="How long to wait for graceful drain before erroring (default 60s).",
    ),
) -> None:
    """Stop the daemon for one vault (SIGTERM + wait, optionally SIGKILL)."""
    config = _resolve_config(config_path, vault_name, None)
    paths = resolve_paths(config.vault_path)
    state = read_state(paths.state_file)
    if state is None:
        typer.echo(f"no daemon running for vault {config.vault_name}")
        return

    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"daemon for {config.vault_name} was already stopped (pid {state.pid})")
        return

    deadline = time.monotonic() + timeout  # pragma: no cover - live-PID wait; smoke-covered
    while time.monotonic() < deadline:  # pragma: no cover
        if not _pid_alive(state.pid):  # pragma: no cover
            typer.echo(f"daemon stopped (pid {state.pid})")  # pragma: no cover
            return  # pragma: no cover
        time.sleep(0.2)  # pragma: no cover

    if force:  # pragma: no cover - force-SIGKILL only after timeout
        with contextlib.suppress(ProcessLookupError):
            os.kill(state.pid, signal.SIGKILL)
        typer.echo(f"daemon SIGKILLed after {timeout:.0f}s (pid {state.pid})")
    else:  # pragma: no cover
        typer.secho(
            f"daemon did not stop within {timeout:.0f}s; pass --force to SIGKILL",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1)


def _build_not_running_status(config: EffectiveConfig) -> dict[str, Any]:
    """Status payload shape when the daemon is not running."""
    paths = resolve_paths(config.vault_path)
    return {
        "vault": {"name": config.vault_name, "path": str(paths.vault)},
        "daemon": {
            "running": False,
            "pid": None,
            "started_at": None,
            "uptime_seconds": None,
            "rss_bytes": None,
        },
        "socket": {"present": paths.socket.exists(), "path": str(paths.socket)},
        "state_file": {"present": paths.state_file.exists(), "path": str(paths.state_file)},
        "activity": None,
        "coordinator": None,
        "log": {
            "path": str(paths.log_file),
            "size_bytes": (paths.log_file.stat().st_size if paths.log_file.exists() else None),
            "present": paths.log_file.exists(),
        },
    }


def _build_running_status(config: EffectiveConfig, state_data: dict[str, Any]) -> dict[str, Any]:
    """Build the running-daemon status payload."""
    paths = resolve_paths(config.vault_path)
    started_at = state_data["started_at"]
    try:
        started_dt = datetime.fromisoformat(started_at)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=UTC)
        uptime_seconds = (datetime.now(UTC) - started_dt).total_seconds()
    except ValueError:
        uptime_seconds = None
    return {
        "vault": {"name": config.vault_name, "path": str(paths.vault)},
        "daemon": {
            "running": True,
            "pid": state_data["pid"],
            "started_at": started_at,
            "uptime_seconds": uptime_seconds,
            "rss_bytes": None,  # Future enhancement: wire psutil for RSS
        },
        "socket": {
            "present": paths.socket.exists(),
            "path": str(paths.socket),
        },
        "state_file": {
            "present": paths.state_file.exists(),
            "path": str(paths.state_file),
        },
        "activity": None,  # Future enhancement: wire from daemon's in-memory counters
        "coordinator": None,
        "log": {
            "path": str(paths.log_file),
            "size_bytes": (paths.log_file.stat().st_size if paths.log_file.exists() else None),
            "present": paths.log_file.exists(),
        },
    }


def _format_status_text(payload: dict[str, Any]) -> str:
    """Render the status payload as the spec Section 10.3 text form."""
    daemon = payload["daemon"]
    sock = payload["socket"]
    state = payload["state_file"]
    log = payload["log"]
    vault = payload["vault"]
    if not daemon["running"]:
        return (
            f"vault     : {vault['name']} ({vault['path']})\n"
            f"daemon    : not running\n"
            f"socket    : not present at {sock['path']}\n"
            f"state file: not present at {state['path']}\n"
            f"hint      : run `engram serve` (auto-spawn) or "
            f"`engram daemon start --vault {vault['name']}`"
        )
    uptime = daemon["uptime_seconds"]
    uptime_str = f"{uptime:.0f}s" if uptime is not None else "?"
    return (
        f"vault     : {vault['name']} ({vault['path']})\n"
        f"daemon pid: {daemon['pid']}\n"
        f"started at: {daemon['started_at']}\n"
        f"uptime    : {uptime_str}\n"
        f"socket    : {sock['path']}\n"
        f"log file  : {log['path']}"
    )


@app.command()
def status(
    config_path: Path | None = typer.Option(  # noqa: B008
        None, "--config", help="Path to a vault's engram.config.yaml."
    ),
    vault_name: str | None = typer.Option(
        None, "--vault", help="Vault name from the per-user vaults list."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show daemon status for one vault."""
    config = _resolve_config(config_path, vault_name, None)
    paths = resolve_paths(config.vault_path)
    state = read_state(paths.state_file)
    if state is None or not _pid_alive(state.pid):
        payload = _build_not_running_status(config)
    else:
        payload = _build_running_status(
            config,
            {"pid": state.pid, "started_at": state.started_at},
        )
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(_format_status_text(payload))


@app.command()
def logs(
    config_path: Path | None = typer.Option(  # noqa: B008
        None, "--config", help="Path to a vault's engram.config.yaml."
    ),
    vault_name: str | None = typer.Option(
        None, "--vault", help="Vault name from the per-user vaults list."
    ),
    tail_count: int = typer.Option(
        200,
        "--tail",
        help="Print the last N lines of the log file (default 200).",
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="Stream new log lines; reopens on rotation (inode change).",
    ),
) -> None:
    """Tail the daemon's log file."""
    config = _resolve_config(config_path, vault_name, None)
    paths = resolve_paths(config.vault_path)
    if not paths.log_file.exists():
        typer.echo(f"no log file at {paths.log_file} (daemon may never have run)")
        return

    if not config.daemon.log_redact_thought_content:
        typer.secho(
            "[engram-daemon DEBUG mode active — log may contain thought content; treat as PII]",
            fg=typer.colors.YELLOW,
        )

    if not follow:
        with paths.log_file.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for line in lines[-tail_count:]:
            typer.echo(line.rstrip("\n"))
        return

    _tail_follow(paths.log_file)


def _tail_follow(log_path: Path) -> None:  # pragma: no cover - infinite-loop tail; interactive only
    """Tail-poll with inode-reopen logic (re-opens on log rotation)."""
    fh = log_path.open("r", encoding="utf-8", errors="replace")
    try:
        fh.seek(0, os.SEEK_END)
        current_inode = os.fstat(fh.fileno()).st_ino
        while True:
            line = fh.readline()
            if line:
                typer.echo(line.rstrip("\n"))
                continue
            time.sleep(0.1)
            try:
                new_stat = log_path.stat()
            except FileNotFoundError:
                continue
            if new_stat.st_ino != current_inode:
                fh.close()
                fh = log_path.open("r", encoding="utf-8", errors="replace")
                current_inode = os.fstat(fh.fileno()).st_ino
    except KeyboardInterrupt:
        return
    finally:
        with contextlib.suppress(OSError):
            fh.close()


def register(parent_app: typer.Typer) -> None:
    """Attach the daemon subcommand group to a parent typer app."""
    parent_app.add_typer(app, name="daemon")


__all__ = ["app", "register"]
