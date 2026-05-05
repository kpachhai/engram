"""Tests for engram.errors - custom exception hierarchy."""

from __future__ import annotations

import pytest

from engram.errors import (
    ConfigError,
    EmbeddingError,
    EngramError,
    IndexError,
    LockError,
    MigrationError,
    SyncError,
    VaultError,
)

ALL_ERRORS = [
    EngramError,
    ConfigError,
    VaultError,
    LockError,
    SyncError,
    IndexError,
    EmbeddingError,
    MigrationError,
]


@pytest.mark.parametrize("cls", ALL_ERRORS)
def test_each_inherits_from_engram_error(cls):
    assert issubclass(cls, EngramError)
    assert issubclass(cls, Exception)


def test_each_has_unique_error_code():
    codes = [cls.error_code for cls in ALL_ERRORS]
    assert len(codes) == len(set(codes)), f"Duplicate error_codes: {codes}"


@pytest.mark.parametrize("cls", ALL_ERRORS)
def test_error_code_is_non_empty_string(cls):
    assert isinstance(cls.error_code, str)
    assert len(cls.error_code) > 0


@pytest.mark.parametrize("cls", ALL_ERRORS)
def test_each_can_be_raised_and_caught(cls):
    with pytest.raises(cls):
        raise cls("boom")


def _chained_raise() -> None:
    try:
        raise ValueError("root cause")
    except ValueError as exc:
        raise VaultError("vault could not be opened") from exc


def test_chaining_preserves_cause():
    with pytest.raises(VaultError) as exc_info:
        _chained_raise()
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert str(exc_info.value.__cause__) == "root cause"


def test_index_error_does_not_collide_with_builtin():
    """engram.errors.IndexError shadows the builtin; verify the relationship is intentional."""
    import builtins

    # mypy proves these are distinct types statically; the runtime check guards
    # against a refactor that re-introduces inheritance from the builtin.
    assert IndexError is not builtins.IndexError  # type: ignore[comparison-overlap]
    assert not issubclass(IndexError, builtins.IndexError)


def test_engram_error_catches_all_subclasses():
    """A single `except EngramError` block must catch any engram error type."""
    for cls in ALL_ERRORS[1:]:  # skip the base itself
        with pytest.raises(EngramError):
            raise cls("test")
