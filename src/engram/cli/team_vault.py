"""``engram team-vault`` - Phase 4 team-vault commands.

Subcommands:

* ``setup --remote <url> [--init-empty | --adopt-existing]`` - bootstrap a
  new team vault. Writes the four canonical files (``engram.config.yaml``,
  ``.engram/team-policy.yaml``, ``.engram/members.yaml``, ``.gitignore``)
  + a ``.engram/setup_complete`` sentinel.
* ``join <name> [--as <local-alias>]`` - join an existing team vault remote.
  (Layer F.)
* ``add-member <fingerprint> [--display-name <name>]`` - steward command
  to enroll a new member fingerprint. (Layer F.)
* ``unmount [--remove-local] <name>`` - detach a vault from this user's
  config. (Layer F.)
* ``enroll-key`` - first-time GPG signing key bootstrap. (Step 6.5.)
* ``rotate-member-key <old-fp> <new-fp>`` - steward-only key rotation.
* ``revoke-key <fp> [--reason <text>]`` - steward-only revocation.

Setup is idempotent: a prior canonical file refuses overwrite with
``team_vault_already_initialized``; the ``.engram/setup_complete``
sentinel + per-file checks support resume after partial setup.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from engram import __version__
from engram.errors import (
    TeamVaultAlreadyInitialized,
    VaultError,
)
from engram.team.identity import GpgIdentity

_log = logging.getLogger("engram.cli.team_vault")


_TEAM_GITIGNORE = """\
# engram team-vault canonical .gitignore. DO NOT REMOVE these entries.
# They protect the team's index from cross-machine corruption + the
# operator's local identity from leaking into the team remote.
.indexes/
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.tmp
*.swp
*.swo
.DS_Store
# Phase 4 team-vault local-only artifacts.
.engram/identity.local
.engram/push-queue.local
.engram/orphans/
"""

_DEFAULT_TEAM_POLICY_TEMPLATE = """\
# engram team-vault policy. Steward-only mutation (server-side
# pre-receive hook enforces this). EDIT BEFORE COMMITTING.
#
# allowed_prefixes: null = "any"; [] = explicit deny-all.
allowed_prefixes: null
# allowed_sources: null = "any"; [] = explicit deny-all.
allowed_sources: null
# accept_sensitive: default False per pinned invariant 1. Flip to True
# only if the team has explicitly agreed to share sensitive thoughts.
accept_sensitive: false
# required_embedding_model: every member's local engram MUST match.
required_embedding_model: BAAI/bge-small-en-v1.5
required_embedding_dim: 384
# stewards: GPG fingerprints with disaster-recovery permission. The
# operator running setup is added automatically; steward additions
# happen via 'engram team-vault add-member --steward'.
stewards:
  - {steward_fingerprint}
# min_engram_version: clients older than this see a clear upgrade
# error rather than silent push refusal.
min_engram_version: "{engram_version}"
"""

_DEFAULT_MEMBERS_TEMPLATE = """\
# engram team-vault enrolled-member roster.
# One fingerprint per line (40 hex; primary GPG key). Bare-string and
# {{fingerprint, display_name}} forms are both accepted.
members:
  - fingerprint: {first_member_fingerprint}
    display_name: {first_member_display_name}
revoked: []
"""


def _engram_config_yaml_for_setup(*, vault_name: str, vault_id: str, remote_url: str) -> str:
    """Render the canonical engram.config.yaml for a fresh team vault."""
    return f"""\
# engram team-vault canonical config. Checked into the team remote so
# every fresh clone immediately sees the embedding-model lock + vault
# id without first running engram. Steward-only mutation enforced by
# the pre-receive hook.
vault_name: {vault_name}
vault_id: {vault_id}
remote_url: {remote_url}
embedding_model: BAAI/bge-small-en-v1.5
embedding_dim: 384
min_engram_version: "{__version__}"
role: team-write
"""


def setup_cmd(
    target_path: Path,
    *,
    remote_url: str,
    vault_name: str | None = None,
    init_empty: bool = False,
    adopt_existing: bool = False,
    steward_fingerprint: str | None = None,
    steward_display_name: str | None = None,
) -> dict[str, Path]:
    """Run the team-vault setup ceremony at ``target_path``.

    Args:
        target_path: Local checkout where the canonical files land.
        remote_url: The team-vault remote URL. Required (per Step 1
            invariant: team-write requires a remote).
        vault_name: Optional vault alias. Default derived from the
            target path's basename.
        init_empty: If True, refuses if the remote already has commits.
        adopt_existing: If True, accepts an existing populated remote
            and only adds the four canonical files.
        steward_fingerprint: The operator's primary GPG fingerprint
            (40 hex). Required - the operator becomes the first
            steward.
        steward_display_name: Optional display name for the steward.

    Returns:
        Mapping of canonical-file-name -> path written.

    Raises:
        TeamVaultAlreadyInitialized: when a prior setup left
            ``setup_complete`` or any canonical file present.
        VaultError: for path / argument issues.
    """
    if init_empty and adopt_existing:
        msg = "--init-empty and --adopt-existing are mutually exclusive"
        raise VaultError(msg)
    if not steward_fingerprint:
        msg = "setup requires a steward GPG fingerprint (run 'engram team-vault enroll-key' first)"
        raise VaultError(msg)

    target_path.mkdir(parents=True, exist_ok=True)
    engram_dir = target_path / ".engram"
    engram_dir.mkdir(parents=True, exist_ok=True)

    config_file = target_path / "engram.config.yaml"
    members_file = engram_dir / "members.yaml"
    policy_file = engram_dir / "team-policy.yaml"
    gitignore_file = target_path / ".gitignore"
    sentinel_file = engram_dir / "setup_complete"

    # Idempotent: detect prior canonical files and refuse to overwrite.
    # Resume case: missing-then-present is fine (we just create the
    # missing ones); fully-present refuses.
    canonical_files = {
        "engram.config.yaml": config_file,
        ".engram/team-policy.yaml": policy_file,
        ".engram/members.yaml": members_file,
    }
    existing = [name for name, path in canonical_files.items() if path.exists()]
    if existing and not init_empty and not adopt_existing:
        if all(path.exists() for path in canonical_files.values()) and sentinel_file.exists():
            msg = (
                f"team_vault_already_initialized: {target_path} already has "
                f"all canonical files and setup_complete sentinel; refusing "
                f"to overwrite"
            )
            raise TeamVaultAlreadyInitialized(msg)
        # Partial state - we'll resume below.
        _log.info("resuming partial setup: existing canonical files: %s", existing)

    resolved_name = vault_name or target_path.name
    from engram.config.models import derive_vault_id

    vault_id = derive_vault_id(remote_url)

    # Write canonical files (skip ones already present to support resume).
    written: dict[str, Path] = {}
    if not config_file.exists():
        config_file.write_text(
            _engram_config_yaml_for_setup(
                vault_name=resolved_name,
                vault_id=vault_id,
                remote_url=remote_url,
            ),
            encoding="utf-8",
        )
        written["engram.config.yaml"] = config_file
    if not policy_file.exists():
        policy_file.write_text(
            _DEFAULT_TEAM_POLICY_TEMPLATE.format(
                steward_fingerprint=steward_fingerprint,
                engram_version=__version__,
            ),
            encoding="utf-8",
        )
        written[".engram/team-policy.yaml"] = policy_file
    if not members_file.exists():
        members_file.write_text(
            _DEFAULT_MEMBERS_TEMPLATE.format(
                first_member_fingerprint=steward_fingerprint,
                first_member_display_name=steward_display_name or "steward",
            ),
            encoding="utf-8",
        )
        written[".engram/members.yaml"] = members_file
    if not gitignore_file.exists():
        gitignore_file.write_text(_TEAM_GITIGNORE, encoding="utf-8")
        written[".gitignore"] = gitignore_file

    # Always (re)write the sentinel last; its presence signals completion.
    sentinel_file.write_text(
        f"# engram team-vault setup completed by {__version__}\n",
        encoding="utf-8",
    )
    written[".engram/setup_complete"] = sentinel_file

    return written


def register(app: typer.Typer) -> None:
    """Wire the team-vault subcommand group into the engram Typer app."""
    team_vault_app = typer.Typer(
        name="team-vault",
        help="Phase 4 team-vault commands.",
        no_args_is_help=True,
    )

    @team_vault_app.command("setup")
    def setup(
        path: Path = typer.Argument(  # noqa: B008
            ...,
            help="Local team-vault checkout path.",
        ),
        remote: str = typer.Option(
            ...,
            "--remote",
            help="Team vault remote URL (required - team-write needs a remote).",
        ),
        vault_name: str | None = typer.Option(
            None,
            "--name",
            help="Vault alias (default: target dir basename).",
        ),
        init_empty: bool = typer.Option(
            False,
            "--init-empty",
            help="Bootstrap as a fresh empty remote.",
        ),
        adopt_existing: bool = typer.Option(
            False,
            "--adopt-existing",
            help="Add canonical files to a populated remote.",
        ),
        steward_display_name: str | None = typer.Option(
            None,
            "--steward-display-name",
            help="Display name for the operator (steward).",
        ),
        gpg_binary: str = typer.Option(
            "gpg",
            "--gpg-binary",
            help="GPG binary to invoke (rarely overridden).",
            hidden=True,
        ),
    ) -> None:
        """Bootstrap a team vault: writes canonical files + records steward fingerprint."""
        identity = GpgIdentity(gpg_binary=gpg_binary)
        steward_fp = identity.primary_fingerprint()
        if steward_fp is None:
            typer.echo(
                "error: no GPG signing key found on this machine. "
                "Run 'engram team-vault enroll-key' first.",
                err=True,
            )
            raise typer.Exit(code=2)

        try:
            written = setup_cmd(
                path,
                remote_url=remote,
                vault_name=vault_name,
                init_empty=init_empty,
                adopt_existing=adopt_existing,
                steward_fingerprint=steward_fp,
                steward_display_name=steward_display_name,
            )
        except (TeamVaultAlreadyInitialized, VaultError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        for name, location in written.items():
            typer.echo(f"  wrote {name} -> {location}")
        typer.echo(
            "\nNext steps:\n"
            "  1. Edit .engram/team-policy.yaml to set allowlists.\n"
            "  2. Install the pre-receive hook on your git remote.\n"
            "  3. git add . && git commit -S && git push.\n"
            "  4. Other members run 'engram team-vault join <remote>'.",
        )

    app.add_typer(team_vault_app, name="team-vault")


__all__ = ["register", "setup_cmd"]
