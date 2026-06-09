"""Tests for the consolidation error subtree in engram.errors."""

from __future__ import annotations

import pytest

from engram.errors import (
    ConsolidateError,
    ConsolidateModelMismatch,
    ConsolidateReportStale,
    ConsolidateVaultBusy,
    ConsolidateVaultTooLarge,
    EngramError,
)

CONSOLIDATE_ERRORS = [
    ConsolidateError,
    ConsolidateVaultBusy,
    ConsolidateReportStale,
    ConsolidateModelMismatch,
    ConsolidateVaultTooLarge,
]


@pytest.mark.parametrize("error_cls", CONSOLIDATE_ERRORS)
def test_inherits_engram_error(error_cls: type[EngramError]):
    assert issubclass(error_cls, EngramError)
    assert issubclass(error_cls, ConsolidateError)


@pytest.mark.parametrize("error_cls", CONSOLIDATE_ERRORS)
def test_error_code_is_stable_snake_case(error_cls: type[EngramError]):
    code = error_cls.error_code
    assert code
    assert code == code.lower()
    assert " " not in code
    assert code.startswith("consolidate")


def test_error_codes_unique():
    codes = [cls.error_code for cls in CONSOLIDATE_ERRORS]
    assert len(codes) == len(set(codes))


def test_catchable_as_engram_error():
    with pytest.raises(EngramError):
        raise ConsolidateVaultBusy("daemon holds the vault; run `engram daemon stop` first")
