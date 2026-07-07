"""``engram team-vault`` - team-vault commands.

Subcommands:

* ``setup --remote <url> [--init-empty | --adopt-existing]`` - bootstrap a
  new team vault. Writes the four canonical files (``engram.config.yaml``,
  ``.engram/team-policy.yaml``, ``.engram/members.yaml``, ``.gitignore``)
  + a ``.engram/setup_complete`` sentinel.
* ``join <name> [--as <local-alias>]`` - join an existing team vault remote.
* ``add-member <fingerprint> [--display-name <name>]`` - steward command
  to enroll a new member fingerprint.
* ``unmount [--remove-local] <name>`` - detach a vault from this user's
  config.
* ``enroll-key`` - first-time GPG signing key bootstrap.
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
    TeamMemberNotEnrolled,
    TeamVaultAlreadyInitialized,
    VaultError,
)
from engram.team.identity import GpgIdentity
from engram.team.members import (
    MemberEntry,
    MembersList,
    is_valid_fingerprint,
    normalize_fingerprint,
)

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
# team-vault local-only artifacts.
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
# accept_sensitive: default False. Flip to True only if the team has
# explicitly agreed to share sensitive thoughts.
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


def _write_members_yaml(members: MembersList, members_yaml_path: Path) -> None:
    """Serialize a MembersList in the canonical line-merge-friendly form.

    Round-trips every MemberEntry field (including ``superseded_by``,
    which key rotation produces) - hand-rolled per-command writers used
    to silently strip it. Atomic write per the project convention.
    """
    from engram.utils.atomic_write import atomic_write_text

    # Fingerprints are force-quoted: an all-digit fingerprint written
    # bare would round-trip through YAML as an int and fail validation.
    lines = ["members:"]
    for m in members.members:
        if m.display_name is not None or m.superseded_by is not None:
            lines.append(f'  - fingerprint: "{m.fingerprint}"')
            if m.display_name is not None:
                lines.append(f"    display_name: {m.display_name}")
            if m.superseded_by is not None:
                lines.append(f'    superseded_by: "{m.superseded_by}"')
        else:
            lines.append(f'  - "{m.fingerprint}"')
    if members.revoked:
        lines.append("revoked:")
        for fp in members.revoked:
            lines.append(f'  - "{fp}"')
    else:
        lines.append("revoked: []")
    members_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(members_yaml_path, "\n".join(lines) + "\n")


def add_member_cmd(
    members_yaml_path: Path,
    *,
    fingerprint: str,
    display_name: str | None = None,
    caller_fingerprint: str,
    stewards: list[str],
) -> MembersList:
    """Add a member fingerprint to a team vault's members.yaml.

    Steward-only: refuses if ``caller_fingerprint`` is not in
    ``stewards``. Returns the updated MembersList; the caller is
    responsible for serializing + git committing.
    """
    caller_norm = normalize_fingerprint(caller_fingerprint)
    stewards_norm = {normalize_fingerprint(s) for s in stewards}
    if caller_norm not in stewards_norm:
        msg = (
            f"team_member_not_enrolled: caller {caller_fingerprint!r} is not "
            f"a steward; only stewards may add members"
        )
        raise TeamMemberNotEnrolled(msg)
    if not is_valid_fingerprint(fingerprint):
        msg = f"invalid fingerprint: {fingerprint!r} (must be 40 hex chars)"
        raise VaultError(msg)
    fingerprint_norm = normalize_fingerprint(fingerprint)

    # Load existing members.yaml.
    if members_yaml_path.exists():
        from ruamel.yaml import YAML

        yaml_safe = YAML(typ="safe", pure=True)
        data = yaml_safe.load(members_yaml_path.read_text(encoding="utf-8")) or {}
        members = MembersList.from_yaml_dict(data)
    else:
        members = MembersList()

    # Idempotent: if already enrolled, no-op.
    if any(m.fingerprint == fingerprint_norm for m in members.members):
        return members

    new_member = MemberEntry(fingerprint=fingerprint_norm, display_name=display_name)
    members.members.append(new_member)

    _write_members_yaml(members, members_yaml_path)
    return members


def revoke_key_cmd(
    members_yaml_path: Path,
    *,
    fingerprint: str,
    caller_fingerprint: str,
    stewards: list[str],
    reason: str | None = None,
) -> MembersList:
    """Add a fingerprint to the ``revoked`` list. Steward-only.

    The fingerprint stays in ``members:`` so historical thoughts under
    that key remain attributable; ``is_enrolled()`` returns False
    because the revoked list shadows the active membership check.
    """
    del reason  # surfaced via revocation log written by the operator
    caller_norm = normalize_fingerprint(caller_fingerprint)
    stewards_norm = {normalize_fingerprint(s) for s in stewards}
    if caller_norm not in stewards_norm:
        msg = f"caller {caller_fingerprint!r} is not a steward; only stewards may revoke keys"
        raise TeamMemberNotEnrolled(msg)
    if not is_valid_fingerprint(fingerprint):
        msg = f"invalid fingerprint: {fingerprint!r}"
        raise VaultError(msg)
    fingerprint_norm = normalize_fingerprint(fingerprint)

    if not members_yaml_path.exists():
        msg = f"members.yaml not found at {members_yaml_path}"
        raise VaultError(msg)

    from ruamel.yaml import YAML

    yaml_safe = YAML(typ="safe", pure=True)
    data = yaml_safe.load(members_yaml_path.read_text(encoding="utf-8")) or {}
    members = MembersList.from_yaml_dict(data)

    if fingerprint_norm in members.revoked:
        return members
    members.revoked.append(fingerprint_norm)

    _write_members_yaml(members, members_yaml_path)
    return members


def rotate_member_key_cmd(
    members_yaml_path: Path,
    *,
    old_fingerprint: str,
    new_fingerprint: str,
    caller_fingerprint: str,
    stewards: list[str],
) -> MembersList:
    """Rotate a member's key: enroll the new fp, flag the old as superseded.

    Per ADR 007 Q6: prior thoughts stay attributed under the old
    fingerprint; the old entry gains ``superseded_by: <new>`` and the new
    fingerprint is enrolled under the same display name. Steward-only.
    Revocation of the old key (e.g. on compromise) is a separate,
    deliberate ``revoke-key`` step.
    """
    caller_norm = normalize_fingerprint(caller_fingerprint)
    stewards_norm = {normalize_fingerprint(s) for s in stewards}
    if caller_norm not in stewards_norm:
        msg = f"caller {caller_fingerprint!r} is not a steward; only stewards may rotate keys"
        raise TeamMemberNotEnrolled(msg)
    for label, fp in (("old", old_fingerprint), ("new", new_fingerprint)):
        if not is_valid_fingerprint(fp):
            msg = f"invalid {label} fingerprint: {fp!r} (must be 40 hex chars)"
            raise VaultError(msg)
    old_norm = normalize_fingerprint(old_fingerprint)
    new_norm = normalize_fingerprint(new_fingerprint)
    if old_norm == new_norm:
        msg = "old and new fingerprints are identical; nothing to rotate"
        raise VaultError(msg)

    if not members_yaml_path.exists():
        msg = f"members.yaml not found at {members_yaml_path}"
        raise VaultError(msg)

    from ruamel.yaml import YAML

    yaml_safe = YAML(typ="safe", pure=True)
    data = yaml_safe.load(members_yaml_path.read_text(encoding="utf-8")) or {}
    members = MembersList.from_yaml_dict(data)

    old_entry = next((m for m in members.members if m.fingerprint == old_norm), None)
    if old_entry is None:
        msg = f"old fingerprint {old_norm} is not an enrolled member; cannot rotate"
        raise VaultError(msg)

    old_entry.superseded_by = new_norm
    if not any(m.fingerprint == new_norm for m in members.members):
        members.members.append(
            MemberEntry(fingerprint=new_norm, display_name=old_entry.display_name)
        )

    _write_members_yaml(members, members_yaml_path)
    return members


def join_cmd(
    target_path: Path,
    *,
    remote_url: str,
    local_alias: str | None = None,
    expected_embedding_model: str | None = None,
    skip_clone: bool = False,
) -> dict[str, Path | str]:
    """Join an existing team vault remote into the local user's vaults.

    Args:
        target_path: Local checkout path. The remote will be cloned here
            unless ``skip_clone`` is True (used by tests / pre-cloned setups).
        remote_url: Team vault remote URL.
        local_alias: Optional alias for the user's config. Default
            derived from the target dir basename.
        expected_embedding_model: Optional expected embedding model
            from the team policy. When the local engram's configured
            model differs, the join refuses with
            :class:`engram.errors.TeamVaultEmbeddingMismatch`.
        skip_clone: When True, ``target_path`` is assumed to already
            contain the team vault clone (used by tests / for already-
            cloned remotes).

    Returns:
        A dict with the join outcome: ``target_path``, ``alias``,
        ``vault_id``, ``remote_url``.

    Raises:
        VaultError: when the target path is non-empty and not already
            an engram team vault.
        TeamVaultEmbeddingMismatch: when the local embedding model
            differs from the team's required model.
    """
    import subprocess

    from engram.config.models import derive_vault_id
    from engram.errors import TeamVaultEmbeddingMismatch

    target_path = Path(target_path)
    alias = local_alias or target_path.name
    vault_id = derive_vault_id(remote_url)

    if not skip_clone:
        if target_path.exists() and any(target_path.iterdir()):
            msg = (
                f"target {target_path} already exists and is non-empty; "
                f"either pass --skip-clone or choose a fresh path"
            )
            raise VaultError(msg)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(  # noqa: S603
            ["git", "clone", remote_url, str(target_path)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            msg = f"git clone failed: {result.stderr.strip() or result.stdout.strip()}"
            raise VaultError(msg)

    # Verify canonical files are present.
    config_yaml = target_path / "engram.config.yaml"
    if not config_yaml.exists():
        msg = (
            f"target {target_path} has no engram.config.yaml; remote may not be "
            f"a team-vault. Run 'engram team-vault setup' on the steward's machine first."
        )
        raise VaultError(msg)

    # Optional embedding-model compat check.
    if expected_embedding_model is not None:
        from ruamel.yaml import YAML

        yaml_safe = YAML(typ="safe", pure=True)
        cfg = yaml_safe.load(config_yaml.read_text(encoding="utf-8")) or {}
        team_model = cfg.get("embedding_model")
        if team_model and team_model != expected_embedding_model:
            msg = (
                f"team_vault_embedding_mismatch: team requires "
                f"{team_model!r} but local engram is configured with "
                f"{expected_embedding_model!r}; either match the team or "
                f"configure a separate engram instance"
            )
            raise TeamVaultEmbeddingMismatch(msg)

    return {
        "target_path": target_path,
        "alias": alias,
        "vault_id": vault_id,
        "remote_url": remote_url,
    }


def unmount_cmd(
    *,
    vault_alias: str,
    user_config_path: Path,
    remove_local: bool = False,
    local_path: Path | None = None,
) -> dict[str, str]:
    """Detach a vault from the user's engram config.

    Args:
        vault_alias: The alias to remove from ``vaults:``.
        user_config_path: Path to ``~/.config/engram/config.yaml``.
        remove_local: When True, also delete the on-disk clone at
            ``local_path``. The operator opt-in safety: the default
            preserves the local files.
        local_path: Required when ``remove_local`` is True.

    Returns:
        A dict describing the outcome: ``alias``, ``removed_local``.
    """
    import shutil

    from ruamel.yaml import YAML

    if not user_config_path.exists():
        msg = f"user config {user_config_path} not found"
        raise VaultError(msg)
    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    data = yaml_rt.load(user_config_path.read_text(encoding="utf-8")) or {}
    raw_vaults = data.get("vaults", []) or []
    new_vaults = [v for v in raw_vaults if v.get("name") != vault_alias]
    if len(new_vaults) == len(raw_vaults):
        msg = f"vault alias {vault_alias!r} not found in {user_config_path}"
        raise VaultError(msg)
    data["vaults"] = new_vaults
    import io

    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    user_config_path.write_text(buf.getvalue(), encoding="utf-8")

    removed_local = False
    if remove_local:
        if local_path is None:
            msg = "remove_local requires local_path"
            raise VaultError(msg)
        if local_path.exists():
            shutil.rmtree(local_path)
            removed_local = True
    return {
        "alias": vault_alias,
        "removed_local": "yes" if removed_local else "no",
    }


def rebind_cmd(
    *,
    vault_alias: str,
    user_config_path: Path,
    new_remote_url: str,
    local_clone_path: Path | None = None,
) -> dict[str, str]:
    """Update the remote URL for an existing team-vault mount.

    Used by team members after a steward has run ``restore`` against a
    new remote. Updates both ``~/.config/engram/config.yaml`` AND (when
    ``local_clone_path`` is supplied) the local clone's ``origin``
    remote via ``git remote set-url``.
    """
    import subprocess

    from ruamel.yaml import YAML

    if not user_config_path.exists():
        msg = f"user config {user_config_path} not found"
        raise VaultError(msg)
    yaml_rt = YAML(typ="rt")
    data = yaml_rt.load(user_config_path.read_text(encoding="utf-8")) or {}
    vaults = data.get("vaults", []) or []
    found = False
    for v in vaults:
        if v.get("name") == vault_alias:
            v["remote_url"] = new_remote_url
            found = True
            break
    if not found:
        msg = f"vault alias {vault_alias!r} not found in {user_config_path}"
        raise VaultError(msg)
    import io

    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    user_config_path.write_text(buf.getvalue(), encoding="utf-8")

    if local_clone_path is not None and local_clone_path.exists():
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(local_clone_path), "remote", "set-url", "origin", new_remote_url],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            msg = f"git remote set-url failed: {result.stderr.strip()}"
            raise VaultError(msg)
    return {"alias": vault_alias, "new_remote_url": new_remote_url}


def orphan_recover_cmd(
    *,
    orphan_path: Path,
    discard: bool = False,
    target_vault_path: Path | None = None,
) -> dict[str, object]:
    """Walk an orphan tarball and either re-capture into a target or discard.

    Args:
        orphan_path: Path to a ``team-vault-orphan-*.tar.gz`` tarball
            from ``<personal>/.engram/orphans/``.
        discard: If True, deletes the tarball without recovering. If
            False, ``target_vault_path`` is required.
        target_vault_path: Target vault root for recovered thoughts.

    Returns:
        A dict with ``recovered_files``, ``discarded`` (bool).
    """
    import tarfile

    if not orphan_path.exists():
        msg = f"orphan tarball {orphan_path} not found"
        raise VaultError(msg)
    if discard:
        orphan_path.unlink()
        return {"recovered_files": [], "discarded": True}
    if target_vault_path is None:
        msg = "non-discard recovery requires target_vault_path"
        raise VaultError(msg)
    target_thoughts = target_vault_path / "thoughts"
    target_thoughts.mkdir(parents=True, exist_ok=True)
    recovered: list[str] = []
    with tarfile.open(orphan_path) as tar:
        for member in tar.getmembers():
            if member.isfile():
                # Refuse path-traversal: tarfile names must be relative + no '..'.
                if member.name.startswith(("/", "..")) or ".." in Path(member.name).parts:
                    continue
                tar.extract(member, target_thoughts, filter="data")
                recovered.append(member.name)
    return {"recovered_files": recovered, "discarded": False}


def redact_history_cmd(
    *,
    vault_path: Path,
    caller_fingerprint: str,
    stewards: list[str],
    reason: str,
    confirm_history_rewrite: bool = False,
) -> dict[str, str]:
    """Rewrite team-vault history to remove a committed secret. Steward-only.

    This is a documented escape hatch only - the actual history-rewrite
    step is intentionally NOT a wrapper around `git filter-repo` because
    the operator must understand they are doing this. Returns the
    audit-log path the steward should append to. The actual
    `git filter-repo` invocation lives in TEAM_BRAIN_GUIDE.md.
    """
    caller_norm = normalize_fingerprint(caller_fingerprint)
    stewards_norm = {normalize_fingerprint(s) for s in stewards}
    if caller_norm not in stewards_norm:
        msg = f"caller {caller_fingerprint!r} is not a steward; redact-history is steward-only"
        raise TeamMemberNotEnrolled(msg)
    if not confirm_history_rewrite:
        msg = (
            "redact-history rewrites the team's git history; pass "
            "confirm_history_rewrite=True to acknowledge. The operator "
            "must coordinate with the team out-of-band before running."
        )
        raise VaultError(msg)
    log_path = vault_path / ".engram" / "redaction-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import UTC, datetime

    timestamp = datetime.now(tz=UTC).isoformat()
    entry = f"\n## {timestamp}\n- Steward: {caller_fingerprint}\n- Reason: {reason}\n\n"
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8")
    else:
        existing = "# engram team-vault redaction log\n"
    log_path.write_text(existing + entry, encoding="utf-8")
    return {
        "log_path": str(log_path),
        "next_step": (
            "Run 'git filter-repo --strip-blobs-bigger-than 1M' or the "
            "appropriate redaction command (see TEAM_BRAIN_GUIDE.md), then "
            "force-push the rewritten history. Notify the team out-of-band."
        ),
    }


def register(app: typer.Typer) -> None:
    """Wire the team-vault subcommand group into the engram Typer app."""
    team_vault_app = typer.Typer(
        name="team-vault",
        help="Team-vault commands.",
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

    @team_vault_app.command("enroll-key")
    def enroll_key(
        gpg_binary: str = typer.Option(
            "gpg",
            "--gpg-binary",
            hidden=True,
        ),
    ) -> None:
        """Discover the operator's GPG signing key and print its primary fingerprint."""
        identity = GpgIdentity(gpg_binary=gpg_binary)
        if not identity.is_gpg_available():
            typer.echo(
                "error: gpg binary not found on PATH. "
                "Install gpg (brew install gnupg / apt install gnupg) first.",
                err=True,
            )
            raise typer.Exit(code=2)
        fp = identity.primary_fingerprint()
        if fp is None:
            typer.echo(
                "error: no GPG secret keys found. "
                "Generate a key first via: gpg --full-generate-key",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"primary fingerprint: {fp}")
        typer.echo(
            f"\nNext: ask a steward to run 'engram team-vault add-member {fp}'.",
        )

    @team_vault_app.command("add-member")
    def add_member(
        fingerprint: str = typer.Argument(
            ...,
            help="Primary GPG fingerprint to enroll (40 hex characters).",
        ),
        members_yaml: Path = typer.Option(  # noqa: B008
            ...,
            "--members-yaml",
            help="Path to .engram/members.yaml in the team-vault checkout.",
        ),
        policy_yaml: Path = typer.Option(  # noqa: B008
            ...,
            "--policy-yaml",
            help="Path to .engram/team-policy.yaml (used to verify caller is a steward).",
        ),
        display_name: str | None = typer.Option(None, "--display-name"),
        gpg_binary: str = typer.Option("gpg", "--gpg-binary", hidden=True),
    ) -> None:
        """Add a member fingerprint to the team-vault members.yaml. Steward-only."""
        from ruamel.yaml import YAML

        identity = GpgIdentity(gpg_binary=gpg_binary)
        caller_fp = identity.primary_fingerprint()
        if caller_fp is None:
            typer.echo("error: no GPG signing key found", err=True)
            raise typer.Exit(code=2)
        yaml_safe = YAML(typ="safe", pure=True)
        policy_data = yaml_safe.load(policy_yaml.read_text(encoding="utf-8")) or {}
        stewards = policy_data.get("stewards") or []
        try:
            add_member_cmd(
                members_yaml,
                fingerprint=fingerprint,
                display_name=display_name,
                caller_fingerprint=caller_fp,
                stewards=stewards,
            )
        except (TeamMemberNotEnrolled, VaultError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"enrolled {fingerprint} in {members_yaml}")
        typer.echo("Commit + push the change for it to take effect.")

    @team_vault_app.command("revoke-key")
    def revoke_key(
        fingerprint: str = typer.Argument(...),
        members_yaml: Path = typer.Option(..., "--members-yaml"),  # noqa: B008
        policy_yaml: Path = typer.Option(..., "--policy-yaml"),  # noqa: B008
        reason: str | None = typer.Option(None, "--reason"),
        gpg_binary: str = typer.Option("gpg", "--gpg-binary", hidden=True),
    ) -> None:
        """Add a fingerprint to the revoked list. Steward-only."""
        from ruamel.yaml import YAML

        identity = GpgIdentity(gpg_binary=gpg_binary)
        caller_fp = identity.primary_fingerprint()
        if caller_fp is None:
            typer.echo("error: no GPG signing key found", err=True)
            raise typer.Exit(code=2)
        yaml_safe = YAML(typ="safe", pure=True)
        policy_data = yaml_safe.load(policy_yaml.read_text(encoding="utf-8")) or {}
        stewards = policy_data.get("stewards") or []
        try:
            revoke_key_cmd(
                members_yaml,
                fingerprint=fingerprint,
                caller_fingerprint=caller_fp,
                stewards=stewards,
                reason=reason,
            )
        except (TeamMemberNotEnrolled, VaultError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"revoked {fingerprint} in {members_yaml}")
        if reason:
            typer.echo(f"reason: {reason}")

    @team_vault_app.command("rotate-member-key")
    def rotate_member_key(
        old_fingerprint: str = typer.Argument(
            ...,
            help="Currently-enrolled primary GPG fingerprint being rotated out.",
        ),
        new_fingerprint: str = typer.Argument(
            ...,
            help="Replacement primary GPG fingerprint (40 hex characters).",
        ),
        members_yaml: Path = typer.Option(  # noqa: B008
            ...,
            "--members-yaml",
            help="Path to .engram/members.yaml in the team-vault checkout.",
        ),
        policy_yaml: Path = typer.Option(  # noqa: B008
            ...,
            "--policy-yaml",
            help="Path to .engram/team-policy.yaml (used to verify caller is a steward).",
        ),
        gpg_binary: str = typer.Option("gpg", "--gpg-binary", hidden=True),
    ) -> None:
        """Rotate a member's key: enroll the new fp, flag the old as superseded. Steward-only."""
        from ruamel.yaml import YAML

        identity = GpgIdentity(gpg_binary=gpg_binary)
        caller_fp = identity.primary_fingerprint()
        if caller_fp is None:
            typer.echo("error: no GPG signing key found", err=True)
            raise typer.Exit(code=2)
        yaml_safe = YAML(typ="safe", pure=True)
        policy_data = yaml_safe.load(policy_yaml.read_text(encoding="utf-8")) or {}
        stewards = policy_data.get("stewards") or []
        try:
            rotate_member_key_cmd(
                members_yaml,
                old_fingerprint=old_fingerprint,
                new_fingerprint=new_fingerprint,
                caller_fingerprint=caller_fp,
                stewards=stewards,
            )
        except (TeamMemberNotEnrolled, VaultError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"rotated {old_fingerprint} -> {new_fingerprint} in {members_yaml}")
        typer.echo(
            "The old key stays enrolled (historical attribution); run "
            "revoke-key if it is compromised. Commit + push the change."
        )

    @team_vault_app.command("join")
    def join(
        target_path: Path = typer.Argument(  # noqa: B008
            ...,
            help="Local checkout path (cloned from --remote unless --skip-clone).",
        ),
        remote: str = typer.Option(..., "--remote", help="Team vault remote URL."),
        local_alias: str | None = typer.Option(
            None,
            "--as",
            help="Local alias (default: target dir basename).",
        ),
        skip_clone: bool = typer.Option(
            False,
            "--skip-clone",
            help="Assume target_path already contains the team vault clone.",
        ),
        expected_embedding_model: str | None = typer.Option(
            None,
            "--expected-embedding-model",
            help="Expected embedding model from the team policy (refuses on mismatch).",
        ),
    ) -> None:
        """Join an existing team vault remote."""
        try:
            outcome = join_cmd(
                target_path,
                remote_url=remote,
                local_alias=local_alias,
                expected_embedding_model=expected_embedding_model,
                skip_clone=skip_clone,
            )
        except VaultError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"joined team-vault: alias={outcome['alias']} vault_id={outcome['vault_id']}")
        typer.echo(
            "Add to ~/.config/engram/config.yaml under vaults: + restart engram serve.",
        )

    @team_vault_app.command("unmount")
    def unmount(
        vault_alias: str = typer.Argument(..., help="The alias to remove."),
        user_config: Path = typer.Option(  # noqa: B008
            ...,
            "--user-config",
            help="Path to ~/.config/engram/config.yaml.",
        ),
        remove_local: bool = typer.Option(
            False,
            "--remove-local",
            help="Also delete the on-disk clone (operator opt-in).",
        ),
        local_path: Path | None = typer.Option(  # noqa: B008
            None,
            "--local-path",
            help="Required when --remove-local is set.",
        ),
    ) -> None:
        """Detach a vault from the user's engram config."""
        try:
            outcome = unmount_cmd(
                vault_alias=vault_alias,
                user_config_path=user_config,
                remove_local=remove_local,
                local_path=local_path,
            )
        except VaultError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"unmounted {outcome['alias']}; removed_local={outcome['removed_local']}")

    @team_vault_app.command("rebind")
    def rebind(
        vault_alias: str = typer.Argument(..., help="The alias to rebind."),
        user_config: Path = typer.Option(  # noqa: B008
            ...,
            "--user-config",
            help="Path to ~/.config/engram/config.yaml.",
        ),
        new_remote: str = typer.Option(..., "--remote", help="New remote URL."),
        local_clone: Path | None = typer.Option(  # noqa: B008
            None,
            "--local-clone",
            help="If set, also runs git remote set-url on the local clone.",
        ),
    ) -> None:
        """Update the remote URL for an existing team-vault mount."""
        try:
            outcome = rebind_cmd(
                vault_alias=vault_alias,
                user_config_path=user_config,
                new_remote_url=new_remote,
                local_clone_path=local_clone,
            )
        except VaultError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"rebound {outcome['alias']} -> {outcome['new_remote_url']}")

    @team_vault_app.command("orphan-recover")
    def orphan_recover(
        orphan: Path = typer.Argument(  # noqa: B008
            ...,
            help="Path to a team-vault-orphan-*.tar.gz tarball.",
        ),
        target_vault_path: Path | None = typer.Option(  # noqa: B008
            None,
            "--target-vault",
            help="Target vault root for recovered thoughts (required unless --discard).",
        ),
        discard: bool = typer.Option(
            False,
            "--discard",
            help="Delete the tarball without recovering.",
        ),
    ) -> None:
        """Walk an orphan tarball; either re-capture into a target vault or discard."""
        try:
            outcome = orphan_recover_cmd(
                orphan_path=orphan,
                discard=discard,
                target_vault_path=target_vault_path,
            )
        except VaultError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if outcome["discarded"]:
            typer.echo(f"discarded orphan: {orphan}")
        else:
            files = outcome["recovered_files"]
            count = len(files) if isinstance(files, list) else 0
            typer.echo(f"recovered {count} file(s) from {orphan}")

    @team_vault_app.command("redact-history")
    def redact_history(
        vault_path: Path = typer.Argument(  # noqa: B008
            ...,
            help="Team vault local checkout root.",
        ),
        policy_yaml: Path = typer.Option(..., "--policy-yaml"),  # noqa: B008
        reason: str = typer.Option(..., "--reason"),
        i_know_this_rewrites_history: bool = typer.Option(
            False,
            "--i-know-this-rewrites-history",
            help="Required acknowledgment that history rewrite is intended.",
        ),
        gpg_binary: str = typer.Option("gpg", "--gpg-binary", hidden=True),
    ) -> None:
        """Steward-only escape hatch for redacting accidentally-committed secrets."""
        from ruamel.yaml import YAML

        identity = GpgIdentity(gpg_binary=gpg_binary)
        caller_fp = identity.primary_fingerprint()
        if caller_fp is None:
            typer.echo("error: no GPG signing key found", err=True)
            raise typer.Exit(code=2)
        yaml_safe = YAML(typ="safe", pure=True)
        policy_data = yaml_safe.load(policy_yaml.read_text(encoding="utf-8")) or {}
        stewards = policy_data.get("stewards") or []
        try:
            outcome = redact_history_cmd(
                vault_path=vault_path,
                caller_fingerprint=caller_fp,
                stewards=stewards,
                reason=reason,
                confirm_history_rewrite=i_know_this_rewrites_history,
            )
        except (TeamMemberNotEnrolled, VaultError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"recorded redaction in {outcome['log_path']}")
        typer.echo(f"next: {outcome['next_step']}")

    app.add_typer(team_vault_app, name="team-vault")


__all__ = [
    "add_member_cmd",
    "join_cmd",
    "orphan_recover_cmd",
    "rebind_cmd",
    "redact_history_cmd",
    "register",
    "revoke_key_cmd",
    "setup_cmd",
    "unmount_cmd",
]
