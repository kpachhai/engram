"""Stability tests for the 14 sync doctor check codes."""

from __future__ import annotations

from engram.diagnostics import check_codes


def test_all_codes_are_strings():
    for code in check_codes.ALL_PHASE_2_CHECK_CODES:
        assert isinstance(code, str)
        assert code  # non-empty


def test_all_codes_are_unique():
    codes = list(check_codes.ALL_PHASE_2_CHECK_CODES)
    assert len(codes) == len(set(codes))


def test_expected_count_is_fourteen():
    """Exactly 14 sync check codes."""
    assert len(check_codes.ALL_PHASE_2_CHECK_CODES) == 14


def test_each_code_is_snake_case_lowercase():
    for code in check_codes.ALL_PHASE_2_CHECK_CODES:
        assert code == code.lower()
        assert " " not in code
        # No double underscores or leading/trailing separators.
        assert "__" not in code
        assert not code.startswith("_")
        assert not code.endswith("_")


def test_canonical_codes_are_exported_individually():
    expected_individual_exports = {
        "GIT_VERSION_FLOOR",
        "BRANCH_ALIGNMENT",
        "CONFLICT_MARKERS_PRESENT",
        "CLOUD_SYNC_UNDER_DOTGIT",
        "GITIGNORE_INDEXES",
        "SIGNED_COMMITS_REQUIRED",
        "LFS_DRIFT",
        "AUTOCRLF_DRIFT",
        "SUBMODULE_UNDER_VAULT",
        "GPG_AGENT_REACHABLE",
        "VAULT_IDENTITY_REMOTE_MATCH",
        "SYNC_USER_IDENTITY_SET",
        "WORKING_TREE_DIRTY_AT_STARTUP",
        "READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH",
    }
    for name in expected_individual_exports:
        assert hasattr(check_codes, name), f"missing constant: {name}"
        assert getattr(check_codes, name) in check_codes.ALL_PHASE_2_CHECK_CODES
