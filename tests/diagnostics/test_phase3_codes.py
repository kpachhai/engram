"""Multi-vault doctor check-code constants.

Asserts the 9 multi-vault codes exist, are unique non-empty snake_case
strings, and that ``ALL_PHASE_3_CHECK_CODES`` is a strict superset of
``ALL_PHASE_2_CHECK_CODES``.
"""

from __future__ import annotations

import re

import pytest

from engram.diagnostics.check_codes import (
    AGGREGATOR_MODE,
    ALL_PHASE_2_CHECK_CODES,
    ALL_PHASE_3_CHECK_CODES,
    EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS,
    FRIEND_VAULT_BLOCK_THOUGHT_PRESENT,
    LLM_DAILY_COST_CAP_APPROACHED,
    LLM_PROVIDER_REACHABLE,
    MULTIPLE_PRIMARY_VAULTS,
    READ_ONLY_VAULT_DECLARES_LLM,
    USER_CONFIG_VAULT_NAME_MISMATCH,
    VAULT_PATH_COLLISION,
)

_NEW_CODES = (
    MULTIPLE_PRIMARY_VAULTS,
    VAULT_PATH_COLLISION,
    EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS,
    AGGREGATOR_MODE,
    LLM_PROVIDER_REACHABLE,
    LLM_DAILY_COST_CAP_APPROACHED,
    READ_ONLY_VAULT_DECLARES_LLM,
    FRIEND_VAULT_BLOCK_THOUGHT_PRESENT,
    USER_CONFIG_VAULT_NAME_MISMATCH,
)

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


@pytest.mark.parametrize("code", _NEW_CODES)
def test_phase_3_code_is_non_empty_snake_case_string(code: str) -> None:
    assert isinstance(code, str)
    assert code, "code must be non-empty"
    assert _SNAKE_CASE.fullmatch(code), f"{code!r} is not snake_case"


def test_phase_3_codes_are_unique_among_themselves() -> None:
    assert len(_NEW_CODES) == len(set(_NEW_CODES))


def test_phase_3_codes_are_disjoint_from_phase_2() -> None:
    overlap = set(_NEW_CODES) & set(ALL_PHASE_2_CHECK_CODES)
    assert overlap == set()


def test_phase_3_superset_includes_all_phase_2() -> None:
    assert set(ALL_PHASE_2_CHECK_CODES).issubset(set(ALL_PHASE_3_CHECK_CODES))


def test_phase_3_superset_count() -> None:
    """14 sync codes + 9 multi-vault codes = 23 total, all unique."""
    assert len(ALL_PHASE_3_CHECK_CODES) == len(ALL_PHASE_2_CHECK_CODES) + 9
    assert len(set(ALL_PHASE_3_CHECK_CODES)) == len(ALL_PHASE_3_CHECK_CODES)


def test_phase_3_codes_appear_in_canonical_superset_in_order() -> None:
    """Sync codes come first in canonical superset; multi-vault codes follow."""
    head = ALL_PHASE_3_CHECK_CODES[: len(ALL_PHASE_2_CHECK_CODES)]
    assert tuple(head) == tuple(ALL_PHASE_2_CHECK_CODES)
    tail = ALL_PHASE_3_CHECK_CODES[len(ALL_PHASE_2_CHECK_CODES) :]
    assert set(tail) == set(_NEW_CODES)
