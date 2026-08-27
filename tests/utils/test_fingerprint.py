"""Tests for engram.utils.fingerprint - the canonical body fingerprint."""

from __future__ import annotations

import hashlib

from hypothesis import given, settings
from hypothesis import strategies as st

from engram.utils.fingerprint import EMPTY_FINGERPRINT, compute_fingerprint, normalize_body


def test_empty_body_has_known_sha256():
    expected = hashlib.sha256(b"").hexdigest()
    assert expected == EMPTY_FINGERPRINT
    assert compute_fingerprint("") == expected


def test_whitespace_only_body_normalizes_to_empty():
    assert compute_fingerprint("   \n\n  \n\t\n") == EMPTY_FINGERPRINT


def test_crlf_lf_cr_produce_identical_fingerprint():
    a = "line one\nline two\nline three\n"
    b = "line one\r\nline two\r\nline three\r\n"
    c = "line one\rline two\rline three\r"
    assert compute_fingerprint(a) == compute_fingerprint(b) == compute_fingerprint(c)


def test_trailing_whitespace_per_line_stripped():
    a = "line one\nline two\n"
    b = "line one   \nline two\t\t\n"
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_trailing_blank_lines_stripped():
    a = "line one\nline two"
    b = "line one\nline two\n\n\n"
    c = "line one\nline two\n   \n   \n"
    assert compute_fingerprint(a) == compute_fingerprint(b) == compute_fingerprint(c)


def test_internal_whitespace_preserved():
    """Internal blank lines and indentation must affect the fingerprint."""
    a = "line one\n\nline three"
    b = "line one\nline three"
    assert compute_fingerprint(a) != compute_fingerprint(b)


def test_internal_indentation_preserved():
    a = "line one\n  indented\nline three"
    b = "line one\nindented\nline three"
    assert compute_fingerprint(a) != compute_fingerprint(b)


def test_case_sensitivity():
    assert compute_fingerprint("Hello") != compute_fingerprint("hello")


def test_unicode_content_handled():
    """Body containing CJK / emoji / RTL fingerprints stably."""
    a = compute_fingerprint("世界 🌍 السلام")
    b = compute_fingerprint("世界 🌍 السلام")
    assert a == b
    # Must be different from ASCII transliteration.
    assert a != compute_fingerprint("hello world")


def test_normalize_body_returns_bytes():
    out = normalize_body("hello\n")
    assert isinstance(out, bytes)
    assert out == b"hello"


def test_leading_prefix_line_included_in_fingerprint():
    """The body INCLUDES the leading [Prefix] line."""
    with_prefix = "[Lesson] never use yaml.load"
    without_prefix = "never use yaml.load"
    assert compute_fingerprint(with_prefix) != compute_fingerprint(without_prefix)


@given(content=st.text())
@settings(max_examples=50, deadline=None)
def test_fingerprint_is_deterministic_property(content: str):
    """Same input produces same output across calls."""
    assert compute_fingerprint(content) == compute_fingerprint(content)


@given(content=st.text(min_size=1))
@settings(max_examples=50, deadline=None)
def test_appended_trailing_blank_line_does_not_change_fingerprint(content: str):
    """Spec: trailing blank lines are stripped."""
    a = compute_fingerprint(content)
    b = compute_fingerprint(content + "\n")
    c = compute_fingerprint(content + "\n\n   \n")
    assert a == b == c


@given(content=st.text())
@settings(max_examples=50, deadline=None)
def test_line_ending_swap_does_not_change_fingerprint(content: str):
    """Spec step 2: replace CRLF and CR with LF.

    Hypothesis can generate ``content`` that already contains mixed line
    endings (e.g. ``"\r\n0"``); applying ``replace("\n", "\r\n")`` to
    such an input doubles existing carriage returns and breaks the
    naive three-way comparison. Normalize the input to a single
    canonical (LF-only) form FIRST, then verify that all three
    line-ending transforms hash to the same fingerprint.
    """
    canonical = content.replace("\r\n", "\n").replace("\r", "\n")
    lf = compute_fingerprint(canonical)
    crlf = compute_fingerprint(canonical.replace("\n", "\r\n"))
    cr = compute_fingerprint(canonical.replace("\n", "\r"))
    assert lf == crlf == cr
