"""The closing verdict line of ``engram doctor``.

A report can exit 0 with rows that never ran. The verdict sentence is the
line an operator reads first, so it must not say the same thing in both
cases: "all checks green" over a report holding skips is the false green
the SKIP status was introduced to remove.
"""

from __future__ import annotations

import typer

from engram.cli.doctor import _verdict_line


def test_verdict_is_green_only_when_nothing_was_skipped() -> None:
    message, color = _verdict_line(exit_code=0, skipped=0, total=12)
    assert message == "engram doctor: all checks green"
    assert color == typer.colors.GREEN


def test_verdict_names_the_skipped_count_instead_of_claiming_green() -> None:
    message, color = _verdict_line(exit_code=0, skipped=22, total=37)
    assert "all checks green" not in message
    assert "22 of 37" in message
    assert "did not run" in message
    assert color == typer.colors.BLUE


def test_verdict_reports_warnings_unchanged() -> None:
    message, color = _verdict_line(exit_code=1, skipped=3, total=37)
    assert message == "engram doctor: warnings (operational, with caveats)"
    assert color == typer.colors.YELLOW


def test_verdict_reports_failures_unchanged() -> None:
    message, color = _verdict_line(exit_code=2, skipped=3, total=37)
    assert message == "engram doctor: failures detected"
    assert color == typer.colors.RED


def test_verdict_names_strict_as_the_reason_for_a_non_zero_exit() -> None:
    """Exit 3 with no WARN and no FAIL reads as a failure unless the line says why."""
    message, color = _verdict_line(exit_code=3, skipped=17, total=37)
    assert "--strict" in message
    assert "17 of 37" in message
    assert "did not run" in message
    assert color == typer.colors.RED


def test_verdict_refuses_to_certify_a_report_with_no_rows() -> None:
    """Zero rows is a wiring failure; the verdict must not read like a clean vault."""
    message, color = _verdict_line(exit_code=2, skipped=0, total=0)
    assert "no checks ran at all" in message
    assert "wiring failure" in message
    assert "--config" in message
    assert color == typer.colors.RED
