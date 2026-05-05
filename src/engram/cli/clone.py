"""``engram clone-vault <url> <local_path>`` - safe-clone helper.

Phase 2 Step 14 deliverable. Performs ``git clone --no-checkout``,
removes ``.git/hooks`` BEFORE the checkout phase fires them, then
runs ``git checkout``. This is the R-H1 mitigation: a malicious
``post-checkout`` hook in the cloned repo cannot execute because the
hooks directory is gone before the checkout runs.

The command also writes a starter ``.engram/identity.local`` template
so the operator can tag the new vault before pointing engram at it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from engram.utils.run_command import run_git


def _identity_template(remote_url: str) -> str:
    """Return a starter ``.engram/identity.local`` body."""
    return (
        "# .engram/identity.local - per-vault identity, NOT committed.\n"
        "# Edit `expected_remote_pattern` to lock this vault to the remote URL\n"
        "# pattern that should serve it; engram will refuse to push otherwise.\n"
        "vault_id: change-me\n"
        f"# expected_remote_pattern: '^{remote_url.replace('.', chr(92) + '.')}$'\n"
        "expected_remote_pattern: '^.*$'\n"
        "# user_email: you@example.com\n"
        "# user_name: Your Name\n"
    )


def register(app: typer.Typer) -> None:
    """Attach the ``clone-vault`` subcommand."""

    @app.command(name="clone-vault")
    def clone_vault(
        url: str = typer.Argument(..., help="Remote URL to clone from."),
        local_path: Path = typer.Argument(  # noqa: B008
            ...,
            help="Destination path for the cloned vault.",
        ),
    ) -> None:
        """Clone a vault remote with the post-checkout hook mitigation.

        Refuses if ``local_path`` exists and is non-empty. Runs the three
        steps in order: ``git clone --no-checkout``, ``rm -rf .git/hooks``,
        ``git checkout``. After success, writes a starter
        ``.engram/identity.local`` template.
        """
        local_path = local_path.expanduser().resolve()
        if local_path.exists():
            if any(local_path.iterdir()):
                typer.secho(
                    f"engram clone-vault: refusing to clone into non-empty path {local_path}",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(2)
        else:
            local_path.parent.mkdir(parents=True, exist_ok=True)

        # Step 1: clone without checkout so hooks don't run.
        cp_clone = run_git(
            ["clone", "--no-checkout", url, str(local_path)],
            cwd=local_path.parent,
            check=False,
            capture_output=True,
        )
        if cp_clone.returncode != 0:
            typer.secho(
                f"engram clone-vault: git clone failed: {cp_clone.stderr.strip()}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        # Step 2: nuke the hooks dir BEFORE checkout fires them.
        hooks_dir = local_path / ".git" / "hooks"
        if hooks_dir.exists():
            shutil.rmtree(hooks_dir)
            hooks_dir.mkdir(parents=True, exist_ok=True)

        # Step 3: now safe to checkout.
        cp_checkout = run_git(
            ["checkout"],
            cwd=local_path,
            check=False,
            capture_output=True,
        )
        if cp_checkout.returncode != 0:
            typer.secho(
                f"engram clone-vault: git checkout failed: {cp_checkout.stderr.strip()}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        # Step 4: write the identity-template so the operator can tag the vault.
        identity_dir = local_path / ".engram"
        identity_dir.mkdir(parents=True, exist_ok=True)
        identity_path = identity_dir / "identity.local"
        if not identity_path.exists():
            identity_path.write_text(_identity_template(url))

        typer.echo(
            f"engram clone-vault: ready at {local_path} (edit {identity_path} to tag the vault)"
        )


__all__ = ["register"]
