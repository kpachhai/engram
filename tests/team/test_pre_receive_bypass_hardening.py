"""Push-time bypasses that let content past the pre-receive gate.

The hook is the server-canonical half of the two-layer boundary, so anything
that makes it skip a file, skip a ref, or read policy from the pushed tree is a
hole in the boundary rather than a cosmetic bug.

Each test here names the specific way the gate was evaded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engram.team.server_hooks.pre_receive import (
    _changed_files,
    _coerce_scalar,
    _committer_fingerprint,
    _parse_stdin,
)

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint
OTHER_FP = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"  # pii-allow: synthetic test fingerprint

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=str(cwd),
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )


# === ref parsing: a ref name is not whitespace-delimited text ===


def test_ref_containing_non_ascii_whitespace_is_not_dropped() -> None:
    """git permits U+00A0 in a ref name; ``str.split()`` treats it as a separator.

    Dropping the line means that ref update is never validated at all - every
    content, attribution, and containment check is skipped for it.
    """
    ref = "refs/heads/foo\u00a0bar"  # U+00A0 is legal in a git ref name
    line = f"{'0' * 40} {'1' * 40} {ref}"

    updates = _parse_stdin(line)

    assert len(updates) == 1, "ref update was silently dropped instead of validated"
    assert updates[0].ref == ref


def test_ordinary_ref_still_parses() -> None:
    line = f"{'0' * 40} {'1' * 40} refs/heads/main"
    updates = _parse_stdin(line)
    assert len(updates) == 1
    assert updates[0].ref == "refs/heads/main"


# === path handling: git quotes non-ASCII paths unless told otherwise ===


def test_changed_files_returns_unquoted_non_ascii_paths(tmp_path: Path) -> None:
    """A quoted path fails ``startswith("thoughts/")`` and skips validation."""
    repo = tmp_path / "repo"
    (repo / "thoughts" / "lesson").mkdir(parents=True)
    assert _git(["init", "--initial-branch=main", "."], repo).returncode == 0
    (repo / "thoughts" / "lesson" / "café.md").write_text("x\n")
    assert _git(["add", "-A"], repo).returncode == 0
    assert _git(["commit", "-m", "one", "--no-verify"], repo).returncode == 0
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    paths = _changed_files("0" * 40, head, cwd=str(repo))

    assert "thoughts/lesson/café.md" in paths, (
        f"non-ASCII path came back quoted/escaped: {paths!r}; "
        "the thoughts/ prefix check would skip it"
    )
    assert not any(p.startswith('"') for p in paths)


# === YAML booleans: the hook and the client must agree ===


@pytest.mark.parametrize("falsy", ["no", "No", "NO", "off", "OFF", "n", "false", "False"])
def test_yaml_11_falsy_scalars_coerce_to_false(falsy: str) -> None:
    """``accept_sensitive: no`` read as a truthy string disables the gate.

    PyYAML (the client side) reads these as False, so a string here means the
    two enforcement layers disagree in the unsafe direction.
    """
    assert _coerce_scalar(falsy) is False


@pytest.mark.parametrize("truthy", ["yes", "Yes", "on", "ON", "y", "true", "True"])
def test_yaml_11_truthy_scalars_coerce_to_true(truthy: str) -> None:
    assert _coerce_scalar(truthy) is True


def test_bare_words_are_still_strings() -> None:
    """Only the YAML boolean vocabulary is coerced; other words stay strings."""
    assert _coerce_scalar("maybe") == "maybe"
    assert _coerce_scalar("november") == "november"
    assert _coerce_scalar("yesterday") == "yesterday"


# === signature parsing: anchor on the status line, honour exit status ===


def _fake_verify(stderr: str, returncode: int = 0):
    def _runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout="", stderr=stderr
        )

    return _runner


def test_uid_containing_validsig_cannot_spoof_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key UID is attacker-controlled text on the GOODSIG line.

    Substring-matching "VALIDSIG" over every line lets a crafted UID supply the
    authorization fingerprint before the real VALIDSIG line is ever reached.
    """
    spoofed = (
        f"[GNUPG:] NEWSIG\n"
        f"[GNUPG:] GOODSIG AAAABBBBCCCCDDDD VALIDSIG {OTHER_FP} spoof\n"
        f"[GNUPG:] VALIDSIG {VALID_FP} 2026-01-01 0 0 4 0 1 8 00 {VALID_FP}\n"
    )
    monkeypatch.setattr(subprocess, "run", _fake_verify(spoofed))

    result = _committer_fingerprint("deadbeef", cwd="/fake")

    assert result == VALID_FP, f"authorization principal was spoofed via the key UID: {result}"


def test_failed_verification_yields_no_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonzero ``git verify-commit`` must not produce an authorizing identity."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_verify(f"[GNUPG:] BADSIG {VALID_FP}\n", returncode=1),
    )
    assert _committer_fingerprint("deadbeef", cwd="/fake") is None


def test_revoked_key_signature_yields_no_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """gpg emits VALIDSIG alongside REVKEYSIG, so VALIDSIG alone is not enough."""
    revoked = (
        f"[GNUPG:] REVKEYSIG AAAABBBBCCCCDDDD alice\n"
        f"[GNUPG:] VALIDSIG {VALID_FP} 2026-01-01 0 0 4 0 1 8 00 {VALID_FP}\n"
    )
    monkeypatch.setattr(subprocess, "run", _fake_verify(revoked))
    assert _committer_fingerprint("deadbeef", cwd="/fake") is None


def test_expired_key_signature_yields_no_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    expired = (
        f"[GNUPG:] EXPKEYSIG AAAABBBBCCCCDDDD alice\n"
        f"[GNUPG:] VALIDSIG {VALID_FP} 2026-01-01 0 0 4 0 1 8 00 {VALID_FP}\n"
    )
    monkeypatch.setattr(subprocess, "run", _fake_verify(expired))
    assert _committer_fingerprint("deadbeef", cwd="/fake") is None


def test_subkey_signed_push_still_resolves_primary_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the primary key is the LAST field, not the first."""
    good = (
        f"[GNUPG:] GOODSIG AAAABBBBCCCCDDDD alice\n"
        f"[GNUPG:] VALIDSIG {OTHER_FP} 2026-01-01 0 0 4 0 1 8 00 {VALID_FP}\n"
    )
    monkeypatch.setattr(subprocess, "run", _fake_verify(good))
    assert _committer_fingerprint("deadbeef", cwd="/fake") == VALID_FP
