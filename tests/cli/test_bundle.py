"""``engram export`` and ``engram import`` CLI tests."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from engram.bundle.format import BUNDLE_MANIFEST_FILENAME, BundleManifest
from engram.cli import app
from engram.config import loader as loader_module


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Repoint engram's user-config under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(loader_module, "_USER_CONFIG_DIR", home / ".config" / "engram")
    monkeypatch.setattr(
        loader_module, "_USER_CONFIG_FILE", home / ".config" / "engram" / "config.yaml"
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _write_user_config(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _make_vault(tmp_path: Path, name: str) -> Path:
    vault = tmp_path / name
    (vault / "thoughts").mkdir(parents=True)
    (vault / ".indexes").mkdir(parents=True)
    return vault


def test_export_default_portability_via_cli(fake_home: Path, tmp_path: Path) -> None:
    """engram export with default flags creates a bundle at the requested path."""
    runner = CliRunner()
    vault = _make_vault(tmp_path, "primary_vault")

    _write_user_config(
        loader_module._USER_CONFIG_FILE,
        f"vaults:\n  - name: primary\n    path: {vault}\n    role: primary\n",
    )

    # Capture a thought into the vault directly via the storage facade
    # (bypassing CLI capture commands).
    from engram.storage.facade import VaultStorage
    from engram.storage.sqlite import set_setting

    storage = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        embedding_model_name="BAAI/bge-small-en-v1.5",
        vault_name="primary",
    )
    set_setting(storage.conn, "embedding_model_name", "BAAI/bge-small-en-v1.5")
    storage.capture(
        content="[Pattern] cli-test",
        portability="portable",
        embedding=[1.0] + [0.0] * 383,
    )
    storage.close()

    output = tmp_path / "out.tar.gz"
    result = runner.invoke(app, ["export", "--output", str(output)])
    assert result.exit_code == 0, result.stdout
    assert output.exists()
    with tarfile.open(str(output), mode="r:gz") as tar:
        names = tar.getnames()
        assert BUNDLE_MANIFEST_FILENAME in names
        manifest_member = tar.getmember(BUNDLE_MANIFEST_FILENAME)
        handle = tar.extractfile(manifest_member)
        assert handle is not None
        manifest = BundleManifest.from_json(handle.read().decode("utf-8"))
    assert manifest.portability_filter == ["portable"]


def test_export_invalid_portability_value_refused(fake_home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    vault = _make_vault(tmp_path, "primary_vault")
    _write_user_config(
        loader_module._USER_CONFIG_FILE,
        f"vaults:\n  - name: primary\n    path: {vault}\n    role: primary\n",
    )
    output = tmp_path / "out.tar.gz"
    result = runner.invoke(app, ["export", "--output", str(output), "--portability", "block"])
    assert result.exit_code == 2
    combined = (result.output + (result.stderr if result.stderr_bytes else "")).lower()
    assert "invalid" in combined or "block" in combined


def test_export_refuses_when_lock_held(fake_home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    vault = _make_vault(tmp_path, "primary_vault")
    _write_user_config(
        loader_module._USER_CONFIG_FILE,
        f"vaults:\n  - name: primary\n    path: {vault}\n    role: primary\n",
    )
    # Simulate engram serve holding the per-vault lock.
    lock_path = vault / ".indexes" / "engram.lock"
    lock_path.write_text("held", encoding="utf-8")

    result = runner.invoke(app, ["export", "--output", str(tmp_path / "x.tar.gz")])
    assert result.exit_code == 2
    combined = (result.output + (result.stderr if result.stderr_bytes else "")).lower()
    assert "lock" in combined


def test_import_round_trip_via_cli(fake_home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    src_vault = _make_vault(tmp_path, "source_vault")
    tgt_vault = _make_vault(tmp_path, "target_vault")

    from engram.storage.facade import VaultStorage
    from engram.storage.sqlite import set_setting

    src_storage = VaultStorage(
        thoughts_dir=src_vault / "thoughts",
        index_db_path=src_vault / ".indexes" / "engram.db",
        embedding_model_name="BAAI/bge-small-en-v1.5",
        vault_name="source",
    )
    set_setting(src_storage.conn, "embedding_model_name", "BAAI/bge-small-en-v1.5")
    src_storage.capture(
        content="[Pattern] one",
        portability="portable",
        embedding=[1.0] + [0.0] * 383,
    )
    src_storage.close()

    bundle = tmp_path / "bundle.tar.gz"
    _write_user_config(
        loader_module._USER_CONFIG_FILE,
        (
            "vaults:\n"
            f"  - name: source\n    path: {src_vault}\n    role: primary\n"
            f"  - name: target\n    path: {tgt_vault}\n    role: read-only\n"
        ),
    )
    # Export from source.
    result = runner.invoke(app, ["export", "--output", str(bundle), "--vault", "source"])
    assert result.exit_code == 0, result.stdout

    # Import into target as read-only with --allow-read-only.
    result = runner.invoke(
        app,
        [
            "import",
            str(bundle),
            "--vault",
            "target",
            "--allow-read-only",
        ],
    )
    assert result.exit_code == 0, result.stdout

    # The target vault now has the imported thought + a migration report.
    reports = list((tgt_vault / ".indexes").glob("bundle-import-*.json"))
    assert reports
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["imported_count"] >= 1


def test_import_refuses_read_only_without_allow(fake_home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    src_vault = _make_vault(tmp_path, "source_vault")
    tgt_vault = _make_vault(tmp_path, "target_vault")
    from engram.storage.facade import VaultStorage
    from engram.storage.sqlite import set_setting

    src_storage = VaultStorage(
        thoughts_dir=src_vault / "thoughts",
        index_db_path=src_vault / ".indexes" / "engram.db",
        embedding_model_name="BAAI/bge-small-en-v1.5",
        vault_name="source",
    )
    set_setting(src_storage.conn, "embedding_model_name", "BAAI/bge-small-en-v1.5")
    src_storage.capture(
        content="[Pattern] one",
        portability="portable",
        embedding=[1.0] + [0.0] * 383,
    )
    src_storage.close()

    _write_user_config(
        loader_module._USER_CONFIG_FILE,
        (
            "vaults:\n"
            f"  - name: source\n    path: {src_vault}\n    role: primary\n"
            f"  - name: target\n    path: {tgt_vault}\n    role: read-only\n"
        ),
    )
    bundle = tmp_path / "bundle.tar.gz"
    runner.invoke(app, ["export", "--output", str(bundle), "--vault", "source"])
    result = runner.invoke(app, ["import", str(bundle), "--vault", "target"])
    assert result.exit_code == 2
    combined = result.output + (result.stderr if result.stderr_bytes else "")
    assert "read-only" in combined
