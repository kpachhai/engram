"""Tests for engram.storage.facade - VaultStorage composing markdown + SQLite."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engram.errors import VaultError
from engram.models.mcp import Filter
from engram.storage.facade import VaultStorage, parse_prefix_from_content
from engram.storage.markdown import read_thought
from engram.storage.sqlite_queries import get_thought_row

_DIM = 384


def _zero_vec() -> list[float]:
    return [0.0] * _DIM


def _vec_a() -> list[float]:
    v = [0.0] * _DIM
    v[0] = 1.0
    return v


def _vec_b() -> list[float]:
    v = [0.0] * _DIM
    v[5] = 1.0
    return v


@pytest.fixture
def vault(tmp_path: Path) -> Generator[VaultStorage, None, None]:
    """Spin up a fresh VaultStorage on a tmp path."""
    thoughts_dir = tmp_path / "thoughts"
    indexes_dir = tmp_path / ".indexes"
    thoughts_dir.mkdir()
    indexes_dir.mkdir()
    storage = VaultStorage(
        thoughts_dir=thoughts_dir,
        index_db_path=indexes_dir / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name="BAAI/bge-small-en-v1.5",
        vault_name="default",
    )
    yield storage
    storage.close()


# === parse_prefix_from_content ===


def test_parse_prefix_canonical():
    assert parse_prefix_from_content("[Lesson] body") == "Lesson"
    assert parse_prefix_from_content("[Pattern] body") == "Pattern"


def test_parse_prefix_with_space():
    assert parse_prefix_from_content("[Action Item] do this") == "Action Item"
    assert parse_prefix_from_content("[Session Summary] today") == "Session Summary"


def test_parse_prefix_strips_leading_whitespace():
    assert parse_prefix_from_content("   [Lesson] body") == "Lesson"
    assert parse_prefix_from_content("\n\n[Lesson] body") == "Lesson"


def test_parse_prefix_unknown_value_preserved_verbatim():
    """Per Schema Drift: unknown prefix preserved (not coerced to Note)."""
    assert parse_prefix_from_content("[Brainstorm] my idea") == "Brainstorm"


def test_parse_prefix_no_bracket_returns_note_fallback():
    assert parse_prefix_from_content("plain body without prefix") == "Note"
    assert parse_prefix_from_content("") == "Note"


def test_parse_prefix_malformed_bracket_returns_note():
    assert parse_prefix_from_content("[ ] body") == "Note"
    assert parse_prefix_from_content("[Lesson body") == "Note"


# === capture ===


def test_capture_writes_markdown_and_sqlite_atomically(vault: VaultStorage):
    thought = vault.capture(
        content="[Lesson] when sqlite-vec returns fewer results than k",
        embedding=_zero_vec(),
    )
    assert thought.prefix == "Lesson"
    assert thought.portability == "portable"
    assert thought.fingerprint  # non-empty
    assert thought.file_path.exists()
    assert thought.file_path.read_text().startswith("---\n")

    # Verify SQLite row.
    row = get_thought_row(vault.conn, thought.id)
    assert row is not None
    assert row["embedding_status"] == "ok"


def test_capture_without_embedding_marks_pending(vault: VaultStorage):
    thought = vault.capture(content="[Lesson] no embedding yet")
    row = get_thought_row(vault.conn, thought.id)
    assert row is not None
    assert row["embedding_status"] == "pending"
    cursor = vault.conn.execute(
        "SELECT COUNT(*) FROM thought_embeddings WHERE thought_id = ?",
        (str(thought.id),),
    )
    assert cursor.fetchone()[0] == 0


def test_capture_default_prefix_is_note(vault: VaultStorage):
    thought = vault.capture(content="just a body, no prefix")
    assert thought.prefix == "Note"


def test_capture_metadata_overrides_parsed_prefix(vault: VaultStorage):
    thought = vault.capture(
        content="[Lesson] body",
        prefix="Pattern",  # explicit override wins
    )
    assert thought.prefix == "Pattern"


def test_capture_default_portability_per_prefix_byoc(vault: VaultStorage):
    """[Domain] and [Artifact] default to sensitive per BYOC discipline."""
    domain_thought = vault.capture(content="[Domain] industry vocab")
    assert domain_thought.portability == "sensitive"
    artifact_thought = vault.capture(content="[Artifact] project rationale")
    assert artifact_thought.portability == "sensitive"


def test_capture_explicit_portability_overrides_default(vault: VaultStorage):
    thought = vault.capture(
        content="[Domain] something I want portable anyway",
        portability="portable",
    )
    assert thought.portability == "portable"


def test_capture_with_tags(vault: VaultStorage):
    thought = vault.capture(
        content="[Lesson] body",
        tags=["debugging", "sqlite"],
        embedding=_zero_vec(),
    )
    assert thought.tags == ["debugging", "sqlite"]
    row = get_thought_row(vault.conn, thought.id)
    assert row is not None
    assert row["tags"] == ["debugging", "sqlite"]


def test_capture_uses_default_user_when_no_source_given(vault: VaultStorage):
    # The vault fixture defaults source to None; capture should fill from default_user
    # but our facade requires source explicit unless the caller passes default_user.
    thought = vault.capture(content="[Lesson] body", source="alice")
    assert thought.source == "alice"


def test_capture_round_trip_via_read_thought(vault: VaultStorage):
    """Thought captured + read from disk match in id, content, fingerprint."""
    thought = vault.capture(
        content="[Lesson] round-trip body content here",
        embedding=_zero_vec(),
    )
    result = read_thought(thought.file_path)
    assert result is not None
    read_back, drifts = result
    assert read_back is not None
    assert drifts == []
    assert read_back.id == thought.id
    assert read_back.fingerprint == thought.fingerprint


def test_capture_creates_prefix_subdirectory(vault: VaultStorage):
    thought = vault.capture(content="[Lesson] x", embedding=_zero_vec())
    assert "lesson" in str(thought.file_path)
    assert thought.file_path.parent.name == "lesson"


# === get_by_id ===


def test_get_by_id_returns_thought(vault: VaultStorage):
    captured = vault.capture(content="[Lesson] body", embedding=_zero_vec())
    fetched = vault.get_by_id(captured.id)
    assert fetched is not None
    assert fetched.id == captured.id


def test_get_by_id_unknown_returns_none(vault: VaultStorage):
    from uuid import uuid4

    assert vault.get_by_id(uuid4()) is None


# === list_thoughts ===


def test_list_thoughts_returns_thoughts_and_count(vault: VaultStorage):
    for i in range(5):
        vault.capture(content=f"[Lesson] thought number {i}", embedding=_zero_vec())
    thoughts, total = vault.list_thoughts(limit=3, offset=0)
    assert total == 5
    assert len(thoughts) == 3


def test_list_thoughts_filter_by_prefix(vault: VaultStorage):
    vault.capture(content="[Lesson] one", embedding=_zero_vec())
    vault.capture(content="[Pattern] two", embedding=_zero_vec())
    vault.capture(content="[Lesson] three", embedding=_zero_vec())
    _, total = vault.list_thoughts(filter_=Filter(prefix="Lesson"))
    assert total == 2


def test_list_thoughts_pending_rows_included(vault: VaultStorage):
    """Per R2: pending rows show in list_thoughts (only excluded from search)."""
    vault.capture(content="[Lesson] pending")  # no embedding
    vault.capture(content="[Lesson] ok", embedding=_zero_vec())
    _, total = vault.list_thoughts()
    assert total == 2


# === search ===


def test_search_returns_thoughts_with_similarity(vault: VaultStorage):
    vault.capture(content="[Lesson] near", embedding=_vec_a())
    vault.capture(content="[Lesson] far", embedding=_vec_b())
    results, total_found = vault.search(query_embedding=_vec_a(), k=10)
    assert total_found == 2
    assert len(results) == 2
    # The 'near' thought should have higher similarity than the 'far' one.
    near_results = [r for r in results if r.content.startswith("[Lesson] near")]
    far_results = [r for r in results if r.content.startswith("[Lesson] far")]
    assert near_results
    assert far_results
    assert near_results[0].similarity > far_results[0].similarity


def test_search_excludes_pending_rows(vault: VaultStorage):
    vault.capture(content="[Lesson] pending")  # no embedding
    vault.capture(content="[Lesson] ok", embedding=_vec_a())
    results, total_found = vault.search(query_embedding=_vec_a(), k=10)
    assert total_found == 1
    assert len(results) == 1


# === update + delete ===


def test_update_metadata(vault: VaultStorage):
    thought = vault.capture(content="[Lesson] body", embedding=_zero_vec())
    new_ts = datetime.now(UTC)
    assert vault.update_metadata(
        thought.id,
        prefix="Pattern",
        tags=["new"],
        updated_at=new_ts,
    )
    fetched = vault.get_by_id(thought.id)
    assert fetched is not None
    assert fetched.prefix == "Pattern"
    assert fetched.tags == ["new"]


def test_delete_removes_both_markdown_and_sqlite(vault: VaultStorage):
    thought = vault.capture(content="[Lesson] body", embedding=_zero_vec())
    file_path = thought.file_path
    deleted = vault.delete(thought.id)
    # Return value is the deleted thought (not bool).
    assert deleted.id == thought.id
    assert deleted.prefix == "Lesson"
    assert vault.get_by_id(thought.id) is None
    assert not file_path.exists()


def test_delete_unknown_raises_thought_not_found(vault: VaultStorage):
    from uuid import uuid4

    from engram.errors import ThoughtNotFoundError

    with pytest.raises(ThoughtNotFoundError):
        vault.delete(uuid4())


def test_delete_removes_embedding_row(vault: VaultStorage):
    """The vector index row tied to the deleted thought is removed too."""
    thought = vault.capture(content="[Lesson] vector row test", embedding=_vec_a())

    # Confirm the embedding row exists before delete.
    cur = vault.conn.execute(
        "SELECT thought_id FROM thought_embeddings WHERE thought_id = ?",
        (str(thought.id),),
    )
    assert cur.fetchone() is not None

    vault.delete(thought.id)

    cur = vault.conn.execute(
        "SELECT thought_id FROM thought_embeddings WHERE thought_id = ?",
        (str(thought.id),),
    )
    assert cur.fetchone() is None


def test_delete_emits_audit_log(vault: VaultStorage, caplog: pytest.LogCaptureFixture) -> None:
    """Every deletion emits a structured INFO line via engram.storage.facade."""
    import logging

    thought = vault.capture(content="[Lesson] auditable", embedding=_zero_vec())
    with caplog.at_level(logging.INFO, logger="engram.storage.facade"):
        vault.delete(thought.id, source="cli")
    audit_lines = [
        rec
        for rec in caplog.records
        if rec.name == "engram.storage.facade" and "thought_deleted" in rec.getMessage()
    ]
    assert audit_lines, "expected at least one thought_deleted audit line"
    msg = audit_lines[-1].getMessage()
    assert f"id={thought.id}" in msg
    assert "prefix=Lesson" in msg
    assert "source=cli" in msg


def test_delete_enqueues_sync_coordinator(vault: VaultStorage) -> None:
    """When a coordinator is attached, delete forwards via _post_capture_sync."""
    calls: list[object] = []

    class _StubCoordinator:
        def enqueue(self, path: object, **_kw: object) -> None:
            calls.append(path)

    thought = vault.capture(content="[Lesson] enqueue test", embedding=_zero_vec())
    # Attach AFTER capture so we only see the delete enqueue.
    vault.set_sync_coordinator(_StubCoordinator())
    vault.delete(thought.id)
    assert calls, "expected coordinator.enqueue to be called for the delete"
    # The enqueued path corresponds to the deleted thought's file_path.
    assert calls[-1] == thought.file_path


# === stats ===


def test_stats_empty_vault(vault: VaultStorage):
    stats = vault.stats()
    assert stats.total_count == 0
    assert stats.oldest is None


def test_stats_populated_vault(vault: VaultStorage):
    vault.capture(content="[Lesson] one", source="alice", embedding=_zero_vec())
    vault.capture(content="[Pattern] two", source="alice", embedding=_zero_vec())
    vault.capture(
        content="[Lesson] three",
        source="bob",
        portability="sensitive",
        embedding=_zero_vec(),
    )
    stats = vault.stats()
    assert stats.total_count == 3
    assert stats.by_prefix["Lesson"] == 2
    assert stats.by_prefix["Pattern"] == 1
    assert stats.by_portability.portable == 2
    assert stats.by_portability.sensitive == 1
    assert stats.by_source["alice"] == 2


# === repair_pending_embeddings ===


def test_repair_pending_embeddings_promotes_to_ok(vault: VaultStorage):
    pending = vault.capture(content="[Lesson] no embed yet")
    assert vault.get_by_id(pending.id) is not None

    # Provide a stub embedding-generator function.
    def stub_embed(text: str) -> list[float]:
        del text
        return _zero_vec()

    repaired = vault.repair_pending_embeddings(stub_embed)
    assert repaired == 1

    row = get_thought_row(vault.conn, pending.id)
    assert row is not None
    assert row["embedding_status"] == "ok"


def test_repair_skips_if_no_pending(vault: VaultStorage):
    vault.capture(content="[Lesson] x", embedding=_zero_vec())
    repaired = vault.repair_pending_embeddings(lambda text: _zero_vec())
    assert repaired == 0


# === content size cap (Q1: warn 100KB, reject 1MB) ===


def test_capture_invokes_on_index_failure_callback_on_sqlite_error(
    vault: VaultStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite insert failure fires the callback with thought + exception.

    Regression: prior to 2026-05-18 the daemon was silently swallowing
    SQLite EIOs on capture for 3 days; 38 markdown-only orphan rows
    accumulated before reindex recovered them. The callback channel
    lets the MCP capture handler surface ``index_state='failed'`` to
    AI clients in real time.
    """
    import sqlite3

    from engram.storage import facade as facade_mod

    captured_signals: list[tuple[object, sqlite3.Error]] = []

    def _on_failure(t: object, exc: sqlite3.Error) -> None:
        captured_signals.append((t, exc))

    # Force the SQLite insert to raise once.
    fake_exc = sqlite3.OperationalError("disk I/O error")

    def _raising_insert(*_args: object, **_kw: object) -> None:
        raise fake_exc

    monkeypatch.setattr(facade_mod, "_q_insert_thought", _raising_insert)

    thought = vault.capture(
        content="[Lesson] callback signal test",
        embedding=_zero_vec(),
        on_index_failure=_on_failure,
    )

    # Markdown is on disk (SoT survives the SQLite failure).
    assert thought.file_path.exists()
    # SQLite row is absent (the row insert never landed).
    assert vault.get_by_id(thought.id) is None
    # Callback fired with the right shape.
    assert len(captured_signals) == 1
    signal_thought, signal_exc = captured_signals[0]
    assert getattr(signal_thought, "id", None) == thought.id
    assert signal_exc is fake_exc


def test_capture_callback_exceptions_do_not_propagate(
    vault: VaultStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misbehaving callback must not mask the original capture outcome."""
    import sqlite3

    from engram.storage import facade as facade_mod

    def _raising_insert(*_args: object, **_kw: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(facade_mod, "_q_insert_thought", _raising_insert)

    def _bad_callback(_t: object, _exc: object) -> None:
        raise RuntimeError("callback boom")

    # capture should still return cleanly with the markdown intact.
    thought = vault.capture(
        content="[Lesson] bad callback test",
        embedding=_zero_vec(),
        on_index_failure=_bad_callback,
    )
    assert thought.file_path.exists()


def test_capture_without_callback_preserves_legacy_log_and_continue(
    vault: VaultStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No callback registered: SQLite failure is logged but capture succeeds."""
    import sqlite3

    from engram.storage import facade as facade_mod

    def _raising_insert(*_args: object, **_kw: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(facade_mod, "_q_insert_thought", _raising_insert)

    thought = vault.capture(content="[Lesson] no-callback path", embedding=_zero_vec())
    assert thought.file_path.exists()
    assert vault.get_by_id(thought.id) is None


def test_capture_rejects_oversized_content(vault: VaultStorage):
    huge = "x" * (1 * 1024 * 1024 + 1)  # 1 MB + 1 byte
    with pytest.raises(VaultError, match="too large"):
        vault.capture(content=huge)


# === fingerprint stable across captures of same body ===


def test_capture_same_body_produces_same_fingerprint(vault: VaultStorage):
    a = vault.capture(content="[Lesson] identical body", embedding=_zero_vec())
    b = vault.capture(content="[Lesson] identical body", embedding=_zero_vec())
    assert a.fingerprint == b.fingerprint
    # But UUIDs differ.
    assert a.id != b.id
    # And paths differ (random tail).
    assert a.file_path != b.file_path
