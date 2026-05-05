"""engram CLI entry point.

This module exposes a single Typer ``app`` registered as the ``engram``
console script in :mod:`pyproject.toml`. Subcommands (``init``, ``serve``,
``reindex``, ``doctor``, ``migrate-from-open-brain``) attach to ``app``
from sibling modules in ``engram.cli`` as they are implemented.
"""

from __future__ import annotations

import typer

from engram import __version__

app = typer.Typer(
    name="engram",
    help="Personal AI memory backend - portable, sovereign, protocol-compatible.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    """Print the installed engram version and exit.

    Wired up via ``--version`` / ``-V`` on the root command.
    """
    if value:
        typer.echo(f"engram {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show engram version and exit.",
    ),
) -> None:
    """Root callback. Subcommands attach below as Phase 1 lands them."""
    del version  # consumed via callback


# Register subcommands. Each module exposes a `register(app)` function so we
# can keep this top-level file as the single integration point and avoid
# circular imports.
from engram.cli import doctor as _doctor_cmd  # noqa: E402
from engram.cli import init as _init_cmd  # noqa: E402
from engram.cli import reindex as _reindex_cmd  # noqa: E402
from engram.cli import serve as _serve_cmd  # noqa: E402

_init_cmd.register(app)
_doctor_cmd.register(app)
_reindex_cmd.register(app)
_serve_cmd.register(app)


__all__ = ["app"]
