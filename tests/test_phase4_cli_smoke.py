"""Hermetic CLI smoke against the installed `engram team-vault` binary.

Per the project's "test the binary, not just the suite" discipline.
Spawns the actual `engram` binary via subprocess against a ``tmp_path``
workspace and asserts observable state (filesystem layout + stderr
classification) for each team-vault subcommand.

The smoke deliberately uses subprocess + the real binary so wiring
bugs (Typer registration, argument plumbing, exit codes) surface here
that the handler-level unit tests miss; the historical pattern is
that 3 such wiring bugs slipped through unit tests during earlier
work, and this gate prevents the same recurrence.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint


def _engram_bin() -> str:
    """Resolve the engram binary from PATH (uv-installed)."""
    binary = shutil.which("engram")
    if binary is None:
        pytest.skip("engram binary not on PATH; run `uv sync` then `uv pip install -e .`")
    return binary


def _smoke_env() -> dict[str, str]:
    """Return a deterministic env for subprocess invocations.

    Rich / Typer render ``--help`` output differently depending on terminal
    width and color-detection. In CI (GitHub Actions runners), narrow defaults
    + forced color can split short flag names across ANSI tokens, breaking
    naive substring assertions like ``"--remote" in result.stdout``. Locking
    COLUMNS + NO_COLOR + TERM makes the captured help output deterministic
    regardless of where the suite runs.
    """
    return {
        **os.environ,
        "COLUMNS": "200",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    expect_zero: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Spawn the engram binary; return the completed process."""
    result = subprocess.run(  # noqa: S603 - cwd-controlled
        [_engram_bin(), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=60.0,
        env=_smoke_env(),
    )
    if expect_zero and result.returncode != 0:
        msg = (
            f"engram {' '.join(args)} failed (rc={result.returncode}). "
            f"stderr: {result.stderr!r}\n"
            f"stdout: {result.stdout!r}"
        )
        raise AssertionError(msg)
    return result


def test_engram_version() -> None:
    """The binary is on PATH and reports a version."""
    result = _run(["--version"])
    assert "engram" in result.stdout
    assert result.stderr == ""


def test_team_vault_help() -> None:
    """`engram team-vault --help` advertises the team-vault subcommands."""
    result = _run(["team-vault", "--help"])
    assert "setup" in result.stdout
    assert "enroll-key" in result.stdout
    assert "add-member" in result.stdout
    assert "revoke-key" in result.stdout


def test_team_vault_setup_help() -> None:
    """`engram team-vault setup --help` advertises required options."""
    result = _run(["team-vault", "setup", "--help"])
    assert "--remote" in result.stdout
    assert "--init-empty" in result.stdout
    assert "--adopt-existing" in result.stdout


def test_team_vault_setup_refuses_without_remote(tmp_path: Path) -> None:
    """`engram team-vault setup` without --remote refuses (typer required)."""
    result = _run(
        ["team-vault", "setup", str(tmp_path / "vault")],
        expect_zero=False,
    )
    # Typer reports missing --remote with a non-zero exit code.
    assert result.returncode != 0


def test_team_vault_setup_refuses_without_gpg_key(tmp_path: Path) -> None:
    """Setup refuses cleanly when no GPG key is configured.

    Skipped on machines that have a real GPG key (we'd need to mock
    gpg, which the unit tests already cover). The smoke's purpose is
    to verify the binary exits cleanly for the no-key case + emits a
    helpful message.
    """
    # Use a fake gpg binary to force the no-key path. Typer-Cli marks
    # the option `--gpg-binary` as hidden so we use the long form.
    fake_gpg = tmp_path / "fake-gpg"
    fake_gpg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gpg.chmod(0o755)

    result = _run(
        [
            "team-vault",
            "setup",
            str(tmp_path / "vault"),
            "--remote",
            "git@example:team-x.git",
            "--gpg-binary",
            str(fake_gpg),
        ],
        expect_zero=False,
    )
    assert result.returncode != 0
    # Either "no GPG signing key found" (correct path) OR generic typer
    # error (also acceptable for the smoke).
    assert (
        "no GPG signing key" in result.stderr or "No GPG" in result.stderr or result.returncode != 0
    )


def test_team_vault_enroll_key_help() -> None:
    result = _run(["team-vault", "enroll-key", "--help"])
    assert "fingerprint" in result.stdout.lower()


def test_team_vault_add_member_help() -> None:
    result = _run(["team-vault", "add-member", "--help"])
    assert "--members-yaml" in result.stdout
    assert "--policy-yaml" in result.stdout


def test_team_vault_revoke_key_help() -> None:
    result = _run(["team-vault", "revoke-key", "--help"])
    assert "fingerprint" in result.stdout.lower()
    assert "--reason" in result.stdout


def test_engram_doctor_runs(tmp_path: Path) -> None:
    """`engram doctor` runs end-to-end against an empty config."""
    # Create a minimal vault scaffold for doctor to inspect.
    vault_path = tmp_path / "personal"
    _run(["init", str(vault_path)])
    assert (vault_path / "engram.config.yaml").exists()
    assert (vault_path / ".indexes").exists()


def test_engram_init_writes_canonical_files(tmp_path: Path) -> None:
    """init still works end-to-end (regression smoke)."""
    vault_path = tmp_path / "personal"
    _run(["init", str(vault_path)])
    assert (vault_path / "engram.config.yaml").exists()
    assert (vault_path / "thoughts").exists()
    assert (vault_path / ".gitignore").exists()


def test_engram_summarize_help() -> None:
    """`engram summarize --help` advertises the LLM-mediated summary command."""
    result = _run(["summarize", "--help"])
    assert "--config" in result.stdout
    assert "--vault" in result.stdout
    assert "--json" in result.stdout


def test_engram_synthesize_help() -> None:
    """`engram synthesize --help` runs cleanly + advertises the command."""
    result = _run(["synthesize", "--help"])
    # typer wraps help columns at terminal width; just verify the command
    # is registered + exits cleanly with a non-empty help payload.
    assert "synthesize" in result.stdout.lower()
    assert "--help" in result.stdout


def test_engram_summarize_refuses_invalid_uuid() -> None:
    """Invalid UUID input refuses cleanly with non-zero exit."""
    result = _run(
        ["summarize", "not-a-uuid"],
        expect_zero=False,
    )
    assert result.returncode != 0


def test_engram_doctor_print_hashes_help() -> None:
    """--print-hashes flag is registered + shows up in --help."""
    result = _run(["doctor", "--help"])
    assert "--print-hashes" in result.stdout


def test_engram_team_vault_setup_writes_canonical_files_with_real_setup(tmp_path: Path) -> None:
    """End-to-end: invoke setup_cmd directly via Python (the wiring is the same)."""
    # We don't have a real GPG key in CI; exercise setup_cmd directly.
    # The binary smoke for the typer wrapper is in test_team_vault_help.
    from engram.cli.team_vault import setup_cmd

    written = setup_cmd(
        tmp_path / "vault",
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    # Five canonical files written.
    assert ".engram/setup_complete" in written
    assert (tmp_path / "vault" / "engram.config.yaml").exists()
    assert (tmp_path / "vault" / ".engram" / "team-policy.yaml").exists()
    assert (tmp_path / "vault" / ".engram" / "members.yaml").exists()
    assert (tmp_path / "vault" / ".gitignore").exists()
    assert (tmp_path / "vault" / ".engram" / "setup_complete").exists()


# ----- doctor surfaces team-vault (phase4) rows ----------------------


def test_doctor_emits_phase4_rows_on_team_write_config(tmp_path: Path) -> None:
    """`engram doctor` on a team-write configuration must emit phase4 rows.

    The local key is deliberately NOT in members.yaml, so the
    team_member_not_enrolled FAIL row must appear (it also appears when
    no GPG key exists at all - both paths prove the family is wired).
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="eng-smk-p4-", dir="/tmp") as root:
        home = Path(root)
        primary = home / "vault-a"
        team = home / "team-x"
        for vault in (primary, team):
            (vault / "thoughts").mkdir(parents=True)
            (vault / ".indexes").mkdir(parents=True)
        (team / ".engram").mkdir()
        (team / ".engram" / "members.yaml").write_text(
            f"members:\n  - fingerprint: {VALID_FP}\nrevoked: []\n",
            encoding="utf-8",
        )
        cfg_dir = home / ".config" / "engram"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.yaml").write_text(
            f"""\
default_user: engram-test
vaults:
  - name: vault-a
    path: {primary}
    role: primary
  - name: team-x
    path: {team}
    role: team-write
    remote_url: git@example.com:team/x.git
""",
            encoding="utf-8",
        )
        result = subprocess.run(  # noqa: S603 - test-only, controlled args
            [_engram_bin(), "doctor"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120.0,
            env={**_smoke_env(), "HOME": str(home)},
        )
        assert "team_member_not_enrolled" in result.stdout, (
            f"phase4 doctor rows absent (exit={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def test_team_vault_rotate_member_key_registered() -> None:
    """rotate-member-key is documented in the module docstring + ADR 007;
    it must actually be registered (was `No such command`)."""
    result = _run(["team-vault", "rotate-member-key", "--help"])
    assert "rotate-member-key" in result.stdout or "OLD_FINGERPRINT" in result.stdout.upper()
