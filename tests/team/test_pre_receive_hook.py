"""Tests for engram.team.server_hooks.pre_receive.run_hook (Step 13).

Tests the hook logic in-process via a fake git command runner so the
suite stays hermetic (no real git binary required).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from engram.team.server_hooks.pre_receive import (
    _is_indexes_path,
    _is_valid_fingerprint,
    _normalize_fingerprint,
    _parse_simple_yaml,
    _split_frontmatter,
    run_hook,
)

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint
OTHER_FP = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"  # pii-allow: synthetic test fingerprint


# === YAML parser ===


def test_parse_simple_yaml_handles_top_level_scalars() -> None:
    text = """\
key1: value1
key2: 42
key3: true
key4: null
"""
    parsed = _parse_simple_yaml(text)
    assert parsed["key1"] == "value1"
    assert parsed["key2"] == 42
    assert parsed["key3"] is True
    assert parsed["key4"] is None


def test_parse_simple_yaml_handles_lists() -> None:
    text = """\
items:
  - foo
  - bar
  - baz
"""
    parsed = _parse_simple_yaml(text)
    assert parsed["items"] == ["foo", "bar", "baz"]


def test_parse_simple_yaml_handles_dict_lists() -> None:
    text = """\
members:
  - fingerprint: ABC
    display_name: alice
  - fingerprint: DEF
    display_name: bob
"""
    parsed = _parse_simple_yaml(text)
    assert parsed["members"] == [
        {"fingerprint": "ABC", "display_name": "alice"},
        {"fingerprint": "DEF", "display_name": "bob"},
    ]


def test_parse_simple_yaml_handles_quoted_strings() -> None:
    text = 'min_engram_version: "0.4.0"\n'
    parsed = _parse_simple_yaml(text)
    assert parsed["min_engram_version"] == "0.4.0"


def test_parse_simple_yaml_strips_comments() -> None:
    text = """\
key: value  # inline comment
# full-line comment
key2: x
"""
    parsed = _parse_simple_yaml(text)
    assert parsed["key"] == "value"
    assert parsed["key2"] == "x"


def test_parse_simple_yaml_handles_inline_list() -> None:
    text = "items: [a, b, c]\n"
    parsed = _parse_simple_yaml(text)
    assert parsed["items"] == ["a", "b", "c"]


# === Frontmatter splitter ===


def test_split_frontmatter_extracts_yaml_and_body() -> None:
    content = "---\nid: 1234\nprefix: Lesson\n---\nbody content here\n"
    result = _split_frontmatter(content)
    assert result is not None
    fm, body = result
    assert fm["prefix"] == "Lesson"
    assert "body content" in body


def test_split_frontmatter_returns_none_for_no_fence() -> None:
    assert _split_frontmatter("no fence here") is None


# === Fingerprint helpers ===


def test_is_valid_fingerprint() -> None:
    assert _is_valid_fingerprint(VALID_FP)
    assert not _is_valid_fingerprint("too-short")


def test_normalize_fingerprint_uppers() -> None:
    assert _normalize_fingerprint(VALID_FP.lower()) == VALID_FP


# === _is_indexes_path ===


def test_is_indexes_path_top_level() -> None:
    assert _is_indexes_path(".indexes/foo.db")


def test_is_indexes_path_nested() -> None:
    assert _is_indexes_path("subvault/.indexes/foo.db")


def test_is_indexes_path_negative() -> None:
    assert not _is_indexes_path("thoughts/foo.md")


# === run_hook ===


def _legit_thought_md(prefix: str = "Postmortem", captured_by: str = VALID_FP) -> str:
    return (
        "---\n"
        f"id: 11111111-1111-1111-1111-111111111111\n"
        f"prefix: {prefix}\n"
        f"portability: portable\n"
        f"source: engram-test\n"
        f"created_at: 2026-01-01T00:00:00Z\n"
        f"updated_at: 2026-01-01T00:00:00Z\n"
        f"fingerprint: {'a' * 64}\n"
        f"captured_by: {captured_by}\n"
        f"---\n"
        f"[{prefix}] body\n"
    )


def _block_thought_md() -> str:
    return _legit_thought_md().replace("portability: portable", "portability: block")


def _legit_policy_yaml() -> str:
    return f"""\
allowed_prefixes:
  - Postmortem
  - Decision
allowed_sources: null
accept_sensitive: false
required_embedding_model: m
required_embedding_dim: 1
stewards:
  - {VALID_FP}
min_engram_version: "0.4.0"
"""


def _legit_members_yaml() -> str:
    return f"""\
members:
  - fingerprint: {VALID_FP}
    display_name: alice
revoked: []
"""


def _make_git_state(
    *,
    changed_files: dict[str, str],
    policy_yaml: str = "",
    members_yaml: str = "",
    committer_fingerprint: str | None = VALID_FP,
):
    """Return a (ls_tree_mock, changed_mock, committer_mock, ancestor_mock) fixture."""
    files_at_sha = dict(changed_files)
    files_at_sha[".engram/team-policy.yaml"] = policy_yaml or _legit_policy_yaml()
    files_at_sha[".engram/members.yaml"] = members_yaml or _legit_members_yaml()

    def fake_ls_tree(sha: str, path: str, *, cwd: str | None = None) -> str | None:
        return files_at_sha.get(path)

    def fake_changed(*args: object, **_kwargs: object) -> list[str]:
        return list(changed_files.keys())

    def fake_committer(*args: object, **_kwargs: object) -> str | None:
        return committer_fingerprint

    def fake_ancestor_check(*args: object, **_kwargs: object) -> str:
        return ""

    return fake_ls_tree, fake_changed, fake_committer, fake_ancestor_check


def _patch_hook(
    *,
    changed_files: dict[str, str],
    policy_yaml: str = "",
    members_yaml: str = "",
    committer_fingerprint: str | None = VALID_FP,
    ancestor_passes: bool = True,
):
    ls_tree, changed, committer, _ = _make_git_state(
        changed_files=changed_files,
        policy_yaml=policy_yaml,
        members_yaml=members_yaml,
        committer_fingerprint=committer_fingerprint,
    )

    def fake_git_cmd(args: list[str], *, cwd: str | None = None) -> str:
        if "merge-base" in args:
            if ancestor_passes:
                return ""
            msg = "force push"
            raise RuntimeError(msg)
        return ""

    return [
        patch(
            "engram.team.server_hooks.pre_receive._ls_tree_at",
            side_effect=ls_tree,
        ),
        patch(
            "engram.team.server_hooks.pre_receive._changed_files",
            side_effect=changed,
        ),
        patch(
            "engram.team.server_hooks.pre_receive._committer_fingerprint",
            side_effect=committer,
        ),
        patch(
            "engram.team.server_hooks.pre_receive._git_cmd",
            side_effect=fake_git_cmd,
        ),
    ]


def _drive_hook(stdin_text: str, patches: list[object]) -> tuple[int, str]:
    """Apply patches as context managers + run the hook."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)  # type: ignore[arg-type]
        return run_hook(stdin_text=stdin_text, repo_path="/fake/path")


STDIN = "0000000000000000000000000000000000000000 deadbeef refs/heads/main\n"  # pii-allow: zero sha


def test_hook_passes_legit_push() -> None:
    patches = _patch_hook(
        changed_files={"thoughts/2026/legit.md": _legit_thought_md()},
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 0, stderr
    assert stderr == ""


def test_hook_refuses_indexes_path() -> None:
    patches = _patch_hook(
        changed_files={".indexes/foo.db": "binary"},
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "indexes_path_refused" in stderr


def test_hook_refuses_block_portability_in_team() -> None:
    patches = _patch_hook(
        changed_files={"thoughts/blocked.md": _block_thought_md()},
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "block_thought_in_team_vault_disallowed" in stderr


def test_hook_refuses_committer_mismatch() -> None:
    """Thought captured_by != GPG-signed committer fingerprint."""
    patches = _patch_hook(
        changed_files={
            "thoughts/forge.md": _legit_thought_md(captured_by=OTHER_FP),
        },
        committer_fingerprint=VALID_FP,
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "attribution_committer_mismatch" in stderr


def test_hook_refuses_disallowed_prefix() -> None:
    patches = _patch_hook(
        changed_files={"thoughts/wrong.md": _legit_thought_md(prefix="Friction")},
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "prefix_not_allowed" in stderr


def test_hook_refuses_non_steward_policy_mutation() -> None:
    """Non-steward attempting to push a policy.yaml change refuses."""
    patches = _patch_hook(
        changed_files={".engram/team-policy.yaml": _legit_policy_yaml()},
        committer_fingerprint=OTHER_FP,
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "steward_only_mutation" in stderr


def test_hook_allows_steward_policy_mutation() -> None:
    """A steward CAN push a policy.yaml change."""
    patches = _patch_hook(
        changed_files={".engram/team-policy.yaml": _legit_policy_yaml()},
        committer_fingerprint=VALID_FP,
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 0, stderr


def test_hook_force_push_refused() -> None:
    """Non-fast-forward push refuses."""
    stdin = "abc123 def456 refs/heads/main\n"
    patches = _patch_hook(
        changed_files={"thoughts/legit.md": _legit_thought_md()},
        ancestor_passes=False,
    )
    code, stderr = _drive_hook(stdin, patches)
    assert code == 1
    assert "non_fast_forward_refused" in stderr


def test_hook_lists_all_violations_not_just_first() -> None:
    """A push with multiple violating files lists ALL violations."""
    patches = _patch_hook(
        changed_files={
            "thoughts/v1.md": _block_thought_md(),
            "thoughts/v2.md": _legit_thought_md(prefix="Friction"),
        },
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "v1.md" in stderr
    assert "v2.md" in stderr


def test_hook_skips_non_thought_files() -> None:
    """Non-thought files (README, etc.) are not validated."""
    patches = _patch_hook(
        changed_files={"README.md": "# This is the team README\n"},
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 0, stderr


def test_hook_empty_stdin_passes() -> None:
    code, stderr = run_hook(stdin_text="")
    assert code == 0
    assert stderr == ""


@pytest.mark.parametrize(
    "fingerprint",
    [VALID_FP.lower(), VALID_FP.upper(), VALID_FP],
)
def test_committer_fingerprint_normalization(fingerprint: str) -> None:
    """The hook normalizes captured_by + committer fp to the same case."""
    patches = _patch_hook(
        changed_files={
            "thoughts/x.md": _legit_thought_md(captured_by=fingerprint),
        },
        committer_fingerprint=VALID_FP,
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 0, stderr


def test_committer_fingerprint_uses_primary_not_signing_subkey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VALIDSIG field 1 is the signing (sub)key; the LAST field is the primary.

    Regression: taking the first 40-hex token returned the signing-subkey
    fingerprint, so captured_by (a primary fp) never matched and every
    subkey-signed push was rejected.
    """
    from engram.team.server_hooks import pre_receive

    subkey_fp = OTHER_FP
    primary_fp = VALID_FP
    raw = (
        "[GNUPG:] NEWSIG\n"
        f"[GNUPG:] GOODSIG {subkey_fp[-16:]} Engram Test <t@example.com>\n"
        f"[GNUPG:] VALIDSIG {subkey_fp} 2026-07-07 1751851200 0 4 0 22 8 00 {primary_fp}\n"
        "[GNUPG:] TRUST_ULTIMATE 0 pgp\n"
    )

    class _CompletedProcess:
        returncode = 0
        stdout = ""
        stderr = raw

    monkeypatch.setattr(
        "engram.team.server_hooks.pre_receive.subprocess.run",
        lambda *args, **kwargs: _CompletedProcess(),
    )
    fp = pre_receive._committer_fingerprint("deadbeef")
    assert fp == primary_fp


def test_hook_refuses_thought_missing_captured_by() -> None:
    """Server-canonical attribution: a thought omitting captured_by must refuse.

    Regression: the `if captured_by is not None` guard skipped the whole
    attribution check, accepting hand-edited/pre-team-client thoughts with
    no GPG binding (the exact bypass the server layer exists to reject).
    """
    md = _legit_thought_md().replace(f"captured_by: {VALID_FP}\n", "")
    patches = _patch_hook(changed_files={"thoughts/anon.md": md})
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "attribution_committer_mismatch" in stderr


def test_hook_refuses_revoked_committer() -> None:
    """A revoked key must not be able to push thoughts (members.py promise)."""
    members = f"""\
members:
  - fingerprint: {VALID_FP}
    display_name: alice
revoked:
  - {VALID_FP}
"""
    patches = _patch_hook(
        changed_files={"thoughts/x.md": _legit_thought_md()},
        members_yaml=members,
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "team_membership_revoked" in stderr


def test_hook_refuses_non_enrolled_committer() -> None:
    """A key absent from members.yaml must not be able to push thoughts."""
    patches = _patch_hook(
        changed_files={"thoughts/x.md": _legit_thought_md(captured_by=OTHER_FP)},
        committer_fingerprint=OTHER_FP,
    )
    code, stderr = _drive_hook(STDIN, patches)
    assert code == 1
    assert "team_member_not_enrolled" in stderr
