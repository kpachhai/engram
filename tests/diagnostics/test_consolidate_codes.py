"""Tests for engram.diagnostics.check_codes consolidation additions."""

from __future__ import annotations

import re

from engram.diagnostics.check_codes import (
    ALL_CONSOLIDATE_CHECK_CODES,
    ALL_PHASE_4_CHECK_CODES,
    ARCHIVE_CONFLICT_MARKERS,
    CONSOLIDATE_JOURNAL_ORPHAN,
)


def test_consolidate_tuple_contents() -> None:
    assert set(ALL_CONSOLIDATE_CHECK_CODES) == {
        CONSOLIDATE_JOURNAL_ORPHAN,
        ARCHIVE_CONFLICT_MARKERS,
    }


def test_consolidate_codes_are_unique() -> None:
    assert len(ALL_CONSOLIDATE_CHECK_CODES) == len(set(ALL_CONSOLIDATE_CHECK_CODES))


def test_consolidate_codes_do_not_collide_with_existing() -> None:
    assert not set(ALL_CONSOLIDATE_CHECK_CODES) & set(ALL_PHASE_4_CHECK_CODES)


def test_consolidate_codes_are_snake_case() -> None:
    snake_re = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")
    for code in ALL_CONSOLIDATE_CHECK_CODES:
        assert snake_re.fullmatch(code), f"non-snake_case code: {code!r}"
