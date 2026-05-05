"""Safe subprocess wrapper helpers.

The :func:`run` helper enforces ``shell=False`` (the default) and validates the
``args`` sequence is well-formed strings BEFORE handing to subprocess. The
:func:`run_git` helper additionally pre-stages the four non-interactive
environment variables required by ``02-TECHNICAL_DESIGN.md`` Flow C so git
never prompts for credentials, merge editor input, or LFS smudge.

The helper exists ahead of any sync caller so the sync coordinator builds on
it without having to retrofit env-var hygiene into already-wired call sites.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

#: Non-interactive git environment per ``02-TECHNICAL_DESIGN.md`` Flow C.
GIT_NON_INTERACTIVE_ENV: Final[Mapping[str, str]] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_MERGE_AUTOEDIT": "no",
    "GIT_ASKPASS": "true",
    "GIT_LFS_SKIP_SMUDGE": "1",
}


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with safe defaults.

    ``shell=False`` is enforced. ``args`` must be a sequence of strings; values
    are passed verbatim to the OS process API and are not interpreted by any
    shell. Default ``check=True`` so callers must opt out of the raise-on-error
    behavior intentionally.

    Args:
        args: Command and its arguments as a sequence of strings.
        cwd: Working directory; defaults to the current process working dir.
        env: Environment to set for the subprocess. If ``None``, the parent's
            environment is inherited (Python's default).
        timeout: Optional timeout in seconds.
        check: If ``True`` (default), raise :class:`subprocess.CalledProcessError`
            when the process exits non-zero.
        capture_output: If ``True`` (default), capture stdout and stderr.

    Returns:
        The :class:`subprocess.CompletedProcess` with text mode stdout/stderr.

    Raises:
        TypeError: if ``args`` is not a sequence of strings.
        subprocess.CalledProcessError: when ``check=True`` and exit is non-zero.
        subprocess.TimeoutExpired: when ``timeout`` elapses.
    """
    if not isinstance(args, list | tuple):
        msg = f"args must be a sequence of strings; got {type(args).__name__}"
        raise TypeError(msg)
    for arg in args:
        if not isinstance(arg, str):
            msg = f"args must contain only strings; got {type(arg).__name__}: {arg!r}"
            raise TypeError(msg)

    return subprocess.run(  # noqa: S603 - shell=False (default); args validated above
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        timeout=timeout,
        check=check,
        capture_output=capture_output,
        text=True,
    )


def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 30.0,
    extra_env: Mapping[str, str] | None = None,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``git`` with non-interactive env vars pre-staged.

    Equivalent to :func:`run` but ALWAYS prepends ``"git"`` to ``args``, sets
    the four ``GIT_*`` env vars per ``02-TECHNICAL_DESIGN.md`` Flow C, and
    requires an explicit ``cwd``. The default timeout is 30s so long-running
    git operations cannot hang the calling process indefinitely.

    Args:
        args: Git subcommand and its arguments (do NOT include the leading ``"git"``).
        cwd: Repository working directory (required).
        timeout: Timeout in seconds; default 30.
        extra_env: Additional env vars to merge on top of inherited env + non-interactive defaults.
        check: Raise on non-zero exit; default ``True``.
        capture_output: Capture stdout/stderr; default ``True``.

    Returns:
        The :class:`subprocess.CompletedProcess`.
    """
    full_env = dict(os.environ)
    full_env.update(GIT_NON_INTERACTIVE_ENV)
    if extra_env is not None:
        full_env.update(extra_env)
    return run(
        ["git", *args],
        cwd=cwd,
        env=full_env,
        timeout=timeout,
        check=check,
        capture_output=capture_output,
    )


__all__ = ["GIT_NON_INTERACTIVE_ENV", "run", "run_git"]
