"""Bundle importer tests (Phase 3 Step 10 verifier)."""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from engram.bundle.exporter import BundleExporter
from engram.bundle.format import (
    BUNDLE_MANIFEST_FILENAME,
    BUNDLE_THOUGHTS_DIR,
    BundleManifest,
)
from engram.bundle.importer import BundleImporter
from engram.errors import BundleCycleDetected, BundleImportError
from engram.storage.facade import VaultStorage


def _build_synthetic_bundle(
    *,
    output: Path,
    members: list[tuple[str, bytes]],
    manifest: BundleManifest,
) -> None:
    """Write a tar.gz with arbitrary member names + a manifest."""
    with tarfile.open(str(output), mode="w|gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(datetime.now(UTC).timestamp())
            tar.addfile(info, io.BytesIO(data))
        manifest_bytes = manifest.to_json().encode("utf-8")
        manifest_info = tarfile.TarInfo(name=BUNDLE_MANIFEST_FILENAME)
        manifest_info.size = len(manifest_bytes)
        manifest_info.mtime = int(manifest.exported_at.timestamp())
        tar.addfile(manifest_info, io.BytesIO(manifest_bytes))


def _markdown_member(
    *, thought_id: str, prefix: str = "Pattern", portability: str = "portable"
) -> bytes:
    # Use abcdef so YAML doesn't auto-parse as integer; 64 lowercase hex chars.
    fp = ("a" * 32) + ("b" * 32)
    body = (
        "---\n"
        f"id: {thought_id}\n"
        "schema_version: 1\n"
        f"prefix: {prefix}\n"
        f"portability: {portability}\n"
        "source: friend\n"
        "created_at: 2026-05-05T00:00:00+00:00\n"
        "updated_at: 2026-05-05T00:00:00+00:00\n"
        f"fingerprint: {fp}\n"
        "tags: []\n"
        "vault: source\n"
        "---\n\n"
        f"[{prefix}] body for {thought_id}\n"
    )
    return body.encode("utf-8")


def _make_manifest(*, thought_count: int = 1) -> BundleManifest:
    return BundleManifest(
        schema_version=1,
        source_user="alice",
        source_vault="source",
        exported_at=datetime.now(UTC),
        thought_count=thought_count,
        portability_filter=["portable"],
        embedding_model="BAAI/bge-small-en-v1.5",
        bundle_id=uuid4(),
    )


def test_round_trip_export_then_import(
    source_vault: VaultStorage, target_vault: VaultStorage, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle.tar.gz"
    BundleExporter(storage=source_vault).export_to(bundle)
    result = BundleImporter(target=target_vault).import_into(bundle)
    assert result.imported_count >= 2
    rows, _ = target_vault.list_thoughts(limit=100)
    sources = [r.source for r in rows]
    assert all(s.startswith(f"bundle:{result.manifest.bundle_id}") for s in sources)


def test_path_traversal_rejected(target_vault: VaultStorage, tmp_path: Path) -> None:
    bundle = tmp_path / "bad.tar.gz"
    manifest = _make_manifest(thought_count=1)
    _build_synthetic_bundle(
        output=bundle,
        members=[
            ("../etc/passwd.md", b"oops\n"),
            (f"{BUNDLE_THOUGHTS_DIR}/legit.md", _markdown_member(thought_id=str(uuid4()))),
        ],
        manifest=manifest,
    )
    result = BundleImporter(target=target_vault).import_into(bundle)
    assert any("etc/passwd.md" in name for name in result.rejected_path_traversal)


def test_outside_thoughts_dir_rejected(target_vault: VaultStorage, tmp_path: Path) -> None:
    bundle = tmp_path / "outside.tar.gz"
    manifest = _make_manifest(thought_count=1)
    _build_synthetic_bundle(
        output=bundle,
        members=[
            ("not_thoughts/file.md", _markdown_member(thought_id=str(uuid4()))),
            (f"{BUNDLE_THOUGHTS_DIR}/legit.md", _markdown_member(thought_id=str(uuid4()))),
        ],
        manifest=manifest,
    )
    result = BundleImporter(target=target_vault).import_into(bundle)
    assert any("not_thoughts/" in n for n in result.rejected_outside_thoughts_dir)


def test_block_portability_filtered_at_import(target_vault: VaultStorage, tmp_path: Path) -> None:
    """Friend pushed a block thought; importer drops it pre-merge."""
    bundle = tmp_path / "with_block.tar.gz"
    manifest = _make_manifest(thought_count=2)
    _build_synthetic_bundle(
        output=bundle,
        members=[
            (
                f"{BUNDLE_THOUGHTS_DIR}/block.md",
                _markdown_member(thought_id=str(uuid4()), portability="block"),
            ),
            (
                f"{BUNDLE_THOUGHTS_DIR}/portable.md",
                _markdown_member(thought_id=str(uuid4()), portability="portable"),
            ),
        ],
        manifest=manifest,
    )
    result = BundleImporter(target=target_vault).import_into(bundle)
    assert result.skipped_block_count == 1
    assert result.imported_count == 1


def test_id_collision_refuses_atomically(
    source_vault: VaultStorage, target_vault: VaultStorage, tmp_path: Path
) -> None:
    """A bundle whose manifest has an id colliding with target refuses entirely."""
    # Pre-populate the target with a clean import of source_vault's
    # portable thoughts; pick one of those imported ids as the collision
    # candidate (block / sensitive thoughts get filtered, so picking
    # source_vault.list_thoughts()[0] would risk picking a non-imported
    # thought).
    clean_bundle = tmp_path / "clean.tar.gz"
    BundleExporter(storage=source_vault).export_to(clean_bundle)
    BundleImporter(target=target_vault).import_into(clean_bundle)

    target_rows, _ = target_vault.list_thoughts(limit=10)
    assert target_rows, "expected at least one imported thought in target"
    colliding_id = str(target_rows[0].id)

    bundle = tmp_path / "collide.tar.gz"
    manifest = _make_manifest(thought_count=2)
    _build_synthetic_bundle(
        output=bundle,
        members=[
            (
                f"{BUNDLE_THOUGHTS_DIR}/collide.md",
                _markdown_member(thought_id=colliding_id),
            ),
            (
                f"{BUNDLE_THOUGHTS_DIR}/legit.md",
                _markdown_member(thought_id=str(uuid4())),
            ),
        ],
        manifest=manifest,
    )

    pre_count = target_vault.list_thoughts(limit=1000)[1]
    with pytest.raises(BundleImportError):
        BundleImporter(target=target_vault).import_into(bundle)
    post_count = target_vault.list_thoughts(limit=1000)[1]
    assert pre_count == post_count


def test_cycle_detection_via_bundle_id_chain(
    source_vault: VaultStorage, target_vault: VaultStorage, tmp_path: Path
) -> None:
    bundle = tmp_path / "cycle.tar.gz"
    BundleExporter(storage=source_vault).export_to(bundle)
    # First import: success.
    BundleImporter(target=target_vault).import_into(bundle)
    # Second import of the same bundle into the same target: cycle.
    with pytest.raises(BundleCycleDetected):
        BundleImporter(target=target_vault).import_into(bundle)


def test_unsupported_schema_version_refused(target_vault: VaultStorage, tmp_path: Path) -> None:
    bundle = tmp_path / "vN.tar.gz"
    # Hand-build a manifest with schema_version=2.
    manifest_bytes = json.dumps(
        {
            "schema_version": 2,
            "source_user": "alice",
            "source_vault": "v",
            "exported_at": "2026-05-05T00:00:00+00:00",
            "thought_count": 0,
            "portability_filter": ["portable"],
            "embedding_model": "m",
            "bundle_id": str(uuid4()),
        }
    ).encode("utf-8")
    with tarfile.open(str(bundle), mode="w|gz") as tar:
        info = tarfile.TarInfo(name=BUNDLE_MANIFEST_FILENAME)
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
    with pytest.raises(BundleImportError):
        BundleImporter(target=target_vault).import_into(bundle)


def test_missing_manifest_refused(target_vault: VaultStorage, tmp_path: Path) -> None:
    bundle = tmp_path / "no_manifest.tar.gz"
    with tarfile.open(str(bundle), mode="w|gz") as tar:
        info = tarfile.TarInfo(name=f"{BUNDLE_THOUGHTS_DIR}/x.md")
        data = _markdown_member(thought_id=str(uuid4()))
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(BundleImportError):
        BundleImporter(target=target_vault).import_into(bundle)


def test_import_to_read_only_without_allow_refuses(
    source_vault: VaultStorage, target_vault: VaultStorage, tmp_path: Path
) -> None:
    target_vault.set_read_only_role(read_only=True)
    bundle = tmp_path / "b.tar.gz"
    BundleExporter(storage=source_vault).export_to(bundle)
    with pytest.raises(BundleImportError):
        BundleImporter(target=target_vault).import_into(bundle)


def test_import_to_read_only_with_allow_succeeds(
    source_vault: VaultStorage, target_vault: VaultStorage, tmp_path: Path
) -> None:
    target_vault.set_read_only_role(read_only=True)
    bundle = tmp_path / "b.tar.gz"
    BundleExporter(storage=source_vault).export_to(bundle)
    result = BundleImporter(target=target_vault, allow_read_only=True).import_into(bundle)
    assert result.imported_count >= 2
    # After import, the read-only flag is restored.
    assert target_vault.read_only_role is True


def test_migration_report_written(
    source_vault: VaultStorage, target_vault: VaultStorage, tmp_path: Path
) -> None:
    bundle = tmp_path / "b.tar.gz"
    BundleExporter(storage=source_vault).export_to(bundle)
    result = BundleImporter(target=target_vault).import_into(bundle)
    assert result.migration_report_path is not None
    assert result.migration_report_path.exists()
    payload = json.loads(result.migration_report_path.read_text(encoding="utf-8"))
    assert payload["bundle_id"] == str(result.manifest.bundle_id)
    assert payload["imported_count"] == result.imported_count
