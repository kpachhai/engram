"""Tests for engram.migration.open_brain.

Uses httpx.MockTransport to simulate Open Brain HTTP responses without hitting
the network. Covers F1-F12 edge cases from PHASE_1_PLAN.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from engram.embedding.protocol import EmbeddingProvider
from engram.errors import MigrationError
from engram.migration.open_brain import (
    MigrationConfig,
    OpenBrainClient,
    run_migration,
)
from engram.storage.facade import VaultStorage

_DIM = 384


class _StubEmbedder:
    """Minimal EmbeddingProvider for migration tests."""

    @property
    def model_name(self) -> str:
        return "stub"

    @property
    def dimension(self) -> int:
        return _DIM

    def embed(self, text: str) -> list[float]:
        v = [0.0] * _DIM
        v[hash(text) % _DIM] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)


@pytest.fixture
def vault(tmp_path: Path):
    storage = VaultStorage(
        thoughts_dir=tmp_path / "thoughts",
        index_db_path=tmp_path / ".indexes" / "engram.db",
        embedding_dim=_DIM,
    )
    yield storage
    storage.close()


@pytest.fixture
def embedder() -> EmbeddingProvider:
    return _StubEmbedder()


def _ob_response(thoughts: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a list of thoughts in the JSON-RPC envelope OB returns."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"results": thoughts, "total_count": len(thoughts)},
    }


_PAGE_SIZE = 500


def _make_handler(thought_pages: list[list[dict[str, Any]]]):
    """Build an offset-aware httpx mock handler that mirrors Open Brain's pagination.

    Probe calls (``limit=1``) return the first item from page 0 (or ``[]``).
    Enumerate calls return ``thought_pages[offset // _PAGE_SIZE]`` so the response
    is idempotent under repeated offsets (matching real OB behaviour and letting
    a single handler serve multiple ``run_migration`` invocations on the same
    corpus).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        args = body.get("params", {}).get("arguments", {})
        limit = args.get("limit", _PAGE_SIZE)
        offset = args.get("offset", 0)

        if limit == 1:
            first = thought_pages[0] if thought_pages else []
            return httpx.Response(200, json=_ob_response(first[:1]))

        page_idx = offset // _PAGE_SIZE
        page = thought_pages[page_idx] if page_idx < len(thought_pages) else []
        return httpx.Response(200, json=_ob_response(page))

    return handler


# Patch OpenBrainClient to use injected httpx.Client for tests.
@pytest.fixture
def patched_client():
    """Build an OpenBrainClient backed by a mock transport over the supplied pages."""

    def factory(*pages_list: list[dict[str, Any]]) -> tuple[OpenBrainClient, httpx.Client]:
        handler = _make_handler(list(pages_list))
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, base_url="http://test")
        ob_client = OpenBrainClient("http://test/open-brain-mcp", "k", client=client)
        return ob_client, client

    return factory


# === OpenBrainClient tests ===


def test_open_brain_client_calls_tools_call(patched_client):
    ob, client = patched_client(
        [
            {
                "id": "ob-1",
                "content": "[Lesson] body",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "metadata": {},
            }
        ],
    )
    try:
        results = ob.list_thoughts(limit=1)
        assert len(results) == 1
        assert results[0].id == "ob-1"
        assert results[0].content == "[Lesson] body"
    finally:
        client.close()


def test_open_brain_client_raises_on_error_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "bad"}},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://test") as client:
        ob = OpenBrainClient("http://test/", "key", client=client)
        with pytest.raises(MigrationError, match="returned error"):
            ob.list_thoughts()


def test_open_brain_client_raises_on_http_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://test") as client:
        ob = OpenBrainClient("http://test/", "key", client=client)
        with pytest.raises(MigrationError, match="HTTP 503"):
            ob.list_thoughts()


# === run_migration: F1 empty corpus ===


def test_migration_empty_corpus(vault, embedder, monkeypatch):
    """F1: empty Open Brain corpus -> no-op success."""
    transport = httpx.MockTransport(_make_handler([[]]))

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transport, base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    config = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
    )
    report = run_migration(config)
    assert report.enumerated == 0
    assert report.migrated == 0
    assert report.errors_count == 0


# === F3: exact-triple duplicates skipped on second run ===


def test_migration_idempotent_on_rerun(vault, embedder, monkeypatch, tmp_path):
    page = [
        {
            "id": "ob-1",
            "content": "[Lesson] dedupe me",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        }
    ]
    transport = httpx.MockTransport(_make_handler([page, []]))

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transport, base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    config = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        report_path=tmp_path / "rep1.json",
    )
    report1 = run_migration(config)
    assert report1.migrated == 1
    assert report1.skipped_existing == 0

    # Second run: triple match -> skip.
    config2 = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        report_path=tmp_path / "rep2.json",
    )
    report2 = run_migration(config2)
    assert report2.migrated == 0
    assert report2.skipped_existing == 1


# === F5: future created_at preserved as legacy_created_at ===


def test_migration_future_created_at_preserved(vault, embedder, monkeypatch, tmp_path):
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    page = [
        {
            "id": "ob-future",
            "content": "[Lesson] from the future",
            "created_at": future,
            "updated_at": future,
            "metadata": {"source": "kpachhai"},
        }
    ]
    transport = httpx.MockTransport(_make_handler([page, []]))

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transport, base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    config = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        report_path=tmp_path / "rep.json",
    )
    report = run_migration(config)
    assert report.migrated == 1
    # The captured thought's created_at should be ~now, NOT the future timestamp.
    cursor = vault.conn.execute(
        "SELECT created_at, legacy_created_at FROM thoughts WHERE legacy_id = 'ob-future'"
    )
    row = cursor.fetchone()
    assert row is not None
    captured_created = datetime.fromisoformat(row[0])
    assert captured_created < datetime.now(UTC) + timedelta(seconds=10)
    legacy_created = datetime.fromisoformat(row[1])
    assert legacy_created.year == datetime.now(UTC).year + 1


# === F6: empty body skipped + error logged ===


def test_migration_empty_body_skipped(vault, embedder, monkeypatch, tmp_path):
    page = [
        {
            "id": "ob-empty",
            "content": "   ",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        },
        {
            "id": "ob-good",
            "content": "[Lesson] valid",
            "created_at": "2026-01-02T00:00:00+00:00",
            "updated_at": "2026-01-02T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        },
    ]
    transport = httpx.MockTransport(_make_handler([page, []]))

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transport, base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    config = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        report_path=tmp_path / "rep.json",
    )
    report = run_migration(config)
    assert report.migrated == 1
    assert report.errors_count == 1
    assert any("empty content" in e["error"] for e in report.errors)


# === F8: dry-run reads but writes nothing ===


def test_migration_dry_run_writes_nothing(vault, embedder, monkeypatch, tmp_path):
    page = [
        {
            "id": "ob-1",
            "content": "[Lesson] dry-run probe",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        }
    ]
    transport = httpx.MockTransport(_make_handler([page, []]))

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transport, base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    config = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        dry_run=True,
        report_path=tmp_path / "rep.json",
    )
    report = run_migration(config)
    assert report.migrated == 1
    # No SQLite row written.
    cursor = vault.conn.execute("SELECT COUNT(*) FROM thoughts")
    assert cursor.fetchone()[0] == 0
    # No report file written.
    assert not (tmp_path / "rep.json").exists()


# === F10: --limit caps at N ===


def test_migration_limit_caps_at_n(vault, embedder, monkeypatch, tmp_path):
    page = [
        {
            "id": f"ob-{i}",
            "content": f"[Lesson] number {i}",
            "created_at": f"2026-01-{i + 1:02}T00:00:00+00:00",
            "updated_at": f"2026-01-{i + 1:02}T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        }
        for i in range(5)
    ]
    transport = httpx.MockTransport(_make_handler([page, []]))

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transport, base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    config = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        limit=3,
        report_path=tmp_path / "rep.json",
    )
    report = run_migration(config)
    assert report.migrated == 3


# === F12: --prefer-legacy-id-match in-place update ===


def test_migration_prefer_legacy_id_match_updates_in_place(vault, embedder, monkeypatch, tmp_path):
    """A previously-migrated thought edited at source -> --prefer-legacy-id-match updates."""
    # First migration: capture original content with legacy_id="ob-edit-me".
    page_v1 = [
        {
            "id": "ob-edit-me",
            "content": "[Lesson] original content",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        }
    ]
    page_v2 = [
        {
            "id": "ob-edit-me",
            "content": "[Lesson] EDITED content",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-02-01T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        }
    ]
    transports = {"current": httpx.MockTransport(_make_handler([page_v1, []]))}

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transports["current"], base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    # First run: regular migration.
    cfg1 = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        report_path=tmp_path / "rep1.json",
    )
    rpt1 = run_migration(cfg1)
    assert rpt1.migrated == 1

    cursor = vault.conn.execute(
        "SELECT id, fingerprint FROM thoughts WHERE legacy_id = 'ob-edit-me'"
    )
    row1 = cursor.fetchone()
    assert row1 is not None
    original_engram_id = row1[0]
    original_fp = row1[1]

    # Swap the upstream transport so run 2 sees the edited content.
    transports["current"] = httpx.MockTransport(_make_handler([page_v2, []]))

    # Second run with --prefer-legacy-id-match.
    cfg2 = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        prefer_legacy_id_match=True,
        report_path=tmp_path / "rep2.json",
    )
    rpt2 = run_migration(cfg2)
    assert rpt2.migrated == 1  # in-place update counts as migrated
    assert rpt2.skipped_existing == 0

    cursor = vault.conn.execute(
        "SELECT id, fingerprint FROM thoughts WHERE legacy_id = 'ob-edit-me'"
    )
    row2 = cursor.fetchone()
    assert row2 is not None
    assert row2[0] == original_engram_id  # same id (in-place)
    assert row2[1] != original_fp  # fingerprint refreshed


# === report file structure ===


def test_migration_report_written_to_path(vault, embedder, monkeypatch, tmp_path):
    page = [
        {
            "id": "ob-1",
            "content": "[Lesson] x",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        }
    ]
    transport = httpx.MockTransport(_make_handler([page, []]))

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transport, base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    report_path = tmp_path / "custom-report.json"
    config = MigrationConfig(
        open_brain_url="http://test/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        report_path=report_path,
    )
    run_migration(config)
    assert report_path.exists()
    parsed = json.loads(report_path.read_text())
    assert parsed["totals"]["migrated"] == 1
    assert "migration_id" in parsed
    assert "by_prefix" in parsed
    assert "validation" in parsed


# === migrations audit trail ===


def test_migration_records_audit_trail(vault, embedder, monkeypatch, tmp_path):
    page = [
        {
            "id": "ob-1",
            "content": "[Lesson] x",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"source": "kpachhai"},
        }
    ]
    transport = httpx.MockTransport(_make_handler([page, []]))

    def fake_init(self, url, key, *, client=None, timeout=30.0):
        self._url = url
        self._key = key
        self._client = httpx.Client(transport=transport, base_url="http://test")
        self._owns_client = True
        self._request_id = 0

    monkeypatch.setattr(OpenBrainClient, "__init__", fake_init)

    config = MigrationConfig(
        open_brain_url="http://test-ob/",
        open_brain_key="k",
        vault_storage=vault,
        embedder=embedder,
        default_user="kpachhai",
        report_path=tmp_path / "rep.json",
    )
    run_migration(config)

    cursor = vault.conn.execute(
        "SELECT source_type, source_url, thought_count, error_count FROM migrations"
    )
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "open-brain"
    assert rows[0][1] == "http://test-ob/"
    assert rows[0][2] == 1
    assert rows[0][3] == 0
