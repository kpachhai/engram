"""Tests for engram.logging - structlog configuration writing only to stderr."""

from __future__ import annotations

import json
import logging as stdlib_logging
import sys

import pytest
import structlog

from engram import logging as engram_logging


@pytest.fixture(autouse=True)
def _reset_structlog_state():
    """Reset structlog and stdlib logging state between tests to avoid bleed."""
    yield
    structlog.reset_defaults()
    root = stdlib_logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def test_logs_go_to_stderr_not_stdout(capsys):
    engram_logging.configure_logging(level="INFO", log_format="text")
    log = engram_logging.get_logger("test")
    log.info("hello_event", thing="world")

    captured = capsys.readouterr()
    assert captured.out == "", "MCP stdio reserves stdout; logger must never write there"
    assert "hello_event" in captured.err
    assert "world" in captured.err


def test_secret_keys_are_redacted(capsys):
    engram_logging.configure_logging(level="INFO", log_format="text")
    log = engram_logging.get_logger("test")
    log.info(
        "auth_attempt",
        api_key="sk-leak-1234567890abcdef",
        access_token="tok-2222-leak",
        password="hunter2",
        authorization="Bearer leak-3333",
        regular_field="safe-value",
    )

    captured = capsys.readouterr()
    assert "sk-leak-1234567890abcdef" not in captured.err
    assert "tok-2222-leak" not in captured.err
    assert "hunter2" not in captured.err
    assert "leak-3333" not in captured.err
    assert "<redacted>" in captured.err
    assert "safe-value" in captured.err


def test_x_brain_key_is_redacted(capsys):
    """The Open Brain MCP key header name is x-brain-key per the spec; ensure it redacts."""
    engram_logging.configure_logging(level="INFO", log_format="text")
    log = engram_logging.get_logger("migration")
    log.info("ob_call", **{"x-brain-key": "MUST-NOT-LEAK"})

    captured = capsys.readouterr()
    assert "MUST-NOT-LEAK" not in captured.err
    assert "<redacted>" in captured.err


def test_json_format_produces_parseable_lines(capsys):
    engram_logging.configure_logging(level="INFO", log_format="json")
    log = engram_logging.get_logger("test")
    log.info("event_name", numeric_field=42, str_field="hello")

    captured = capsys.readouterr()
    assert captured.out == ""
    last_line = captured.err.strip().splitlines()[-1]
    parsed = json.loads(last_line)
    assert parsed["event"] == "event_name"
    assert parsed["numeric_field"] == 42
    assert parsed["str_field"] == "hello"


def test_default_level_is_info_and_filters_debug(capsys, monkeypatch):
    monkeypatch.delenv("ENGRAM_LOG_LEVEL", raising=False)
    monkeypatch.delenv("ENGRAM_LOG_FORMAT", raising=False)
    engram_logging.configure_logging()
    log = engram_logging.get_logger("test")
    log.debug("should_not_appear")
    log.info("should_appear")

    captured = capsys.readouterr()
    assert "should_appear" in captured.err
    assert "should_not_appear" not in captured.err


def test_env_var_overrides_default_level(capsys, monkeypatch):
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "DEBUG")
    engram_logging.configure_logging()
    log = engram_logging.get_logger("test")
    log.debug("debug_should_appear")

    captured = capsys.readouterr()
    assert "debug_should_appear" in captured.err


def test_env_var_overrides_format(capsys, monkeypatch):
    monkeypatch.setenv("ENGRAM_LOG_FORMAT", "json")
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "INFO")
    engram_logging.configure_logging()
    log = engram_logging.get_logger("test")
    log.info("via_env")

    captured = capsys.readouterr()
    last_line = captured.err.strip().splitlines()[-1]
    parsed = json.loads(last_line)
    assert parsed["event"] == "via_env"


def test_explicit_kwargs_override_env(capsys, monkeypatch):
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "DEBUG")
    engram_logging.configure_logging(level="WARNING", log_format="text")
    log = engram_logging.get_logger("test")
    log.info("info_should_be_filtered")
    log.warning("warning_should_appear")

    captured = capsys.readouterr()
    assert "info_should_be_filtered" not in captured.err
    assert "warning_should_appear" in captured.err


def test_get_logger_works_without_configure(capsys):
    """Calling get_logger before configure_logging must not crash; default to INFO/text."""
    log = engram_logging.get_logger("test")
    # Calling info() pre-configure should not raise.
    log.info("pre_configure_event")
    # Caller is responsible for stream destination if they bypass configure.
    sys.stderr.flush()
