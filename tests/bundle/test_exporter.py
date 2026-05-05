"""Bundle exporter tests."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from engram.bundle.exporter import BundleExporter
from engram.bundle.format import (
    BUNDLE_MANIFEST_FILENAME,
    BUNDLE_THOUGHTS_DIR,
    BundleManifest,
)
from engram.errors import VaultError
from engram.storage.facade import VaultStorage


def test_export_default_portability_filter(source_vault: VaultStorage, tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    exporter = BundleExporter(storage=source_vault)
    result = exporter.export_to(output)
    assert output.exists()
    assert result.bundle_path == output
    # Default = portable only; sensitive (1) and block (1) are skipped.
    assert result.manifest.portability_filter == ["portable"]
    assert result.manifest.thought_count == 2


def test_export_repeated_portability_includes_sensitive(
    source_vault: VaultStorage, tmp_path: Path
) -> None:
    output = tmp_path / "bundle.tar.gz"
    exporter = BundleExporter(storage=source_vault, portability_filter=("portable", "sensitive"))
    result = exporter.export_to(output)
    assert sorted(result.manifest.portability_filter) == ["portable", "sensitive"]
    assert result.manifest.thought_count == 3


def test_export_refuses_block_portability_in_filter(
    source_vault: VaultStorage,
) -> None:
    with pytest.raises(VaultError):
        BundleExporter(storage=source_vault, portability_filter=("block",))


def test_export_refuses_overwrite(source_vault: VaultStorage, tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    output.write_bytes(b"already here")
    with pytest.raises(VaultError):
        BundleExporter(storage=source_vault).export_to(output)


def test_export_atomic_temp_cleanup_on_rename(source_vault: VaultStorage, tmp_path: Path) -> None:
    """After successful export, no .tmp file should remain."""
    output = tmp_path / "bundle.tar.gz"
    BundleExporter(storage=source_vault).export_to(output)
    tmp_file = output.with_suffix(output.suffix + ".tmp")
    assert not tmp_file.exists()


def test_export_archive_contains_manifest_at_root(
    source_vault: VaultStorage, tmp_path: Path
) -> None:
    output = tmp_path / "bundle.tar.gz"
    BundleExporter(storage=source_vault).export_to(output)
    with tarfile.open(output, mode="r:gz") as tar:
        names = tar.getnames()
    assert BUNDLE_MANIFEST_FILENAME in names
    assert any(name.startswith(f"{BUNDLE_THOUGHTS_DIR}/") for name in names)


def test_export_manifest_can_be_round_tripped(source_vault: VaultStorage, tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    result = BundleExporter(storage=source_vault).export_to(output)
    with tarfile.open(output, mode="r:gz") as tar:
        manifest_member = tar.getmember(BUNDLE_MANIFEST_FILENAME)
        handle = tar.extractfile(manifest_member)
        assert handle is not None
        rebuilt = BundleManifest.from_json(handle.read().decode("utf-8"))
    assert rebuilt.bundle_id == result.manifest.bundle_id


def test_export_empty_vault_produces_valid_bundle(tmp_path: Path) -> None:
    """An empty source vault should yield a valid bundle with thought_count=0."""
    from tests.bundle.conftest import make_vault_storage

    empty = make_vault_storage(base=tmp_path, name="empty")
    output = tmp_path / "empty.tar.gz"
    result = BundleExporter(storage=empty).export_to(output)
    assert result.manifest.thought_count == 0
    assert output.exists()
    empty.close()
