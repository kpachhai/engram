"""Allow ``python -m engram`` to invoke the same typer app as the ``engram`` script.

The proxy's spawn dance (``engram.daemon.client._spawn_daemon_process``)
re-execs the daemon as ``sys.executable -m engram daemon start ...``
because that path is stable: it uses the same Python interpreter the
proxy is running in, with the same site-packages, regardless of how
the user installed engram (``uv tool``, ``pip install -e``, virtualenv,
etc.).

Delegates to :data:`engram.cli.app` (the typer app the console-script
entry point in ``pyproject.toml`` also resolves to), so ``python -m
engram --version`` and ``engram --version`` are identical.
"""

from __future__ import annotations

from engram.cli import app

if __name__ == "__main__":
    app()
