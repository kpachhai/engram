"""Shared fixtures for daemon integration tests.

The integration tests construct a real :class:`DaemonServer` with a
:class:`ServeRuntime` built from real :class:`VaultStorage` plus a
hermetic :class:`FakeEmbedder`. The daemon and any proxy connections
share one asyncio event loop owned by ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path

import pytest

from engram.cli.serve import ServeRuntime
from engram.config.models import (
    AggregatorConfig,
    DaemonConfig,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
)
from engram.daemon.server import DaemonServer
from engram.daemon.socket_paths import SocketPaths, resolve_paths
from engram.mcp.server import build_server
from engram.storage.facade import VaultStorage
from engram.utils.lock import VaultLock


class FakeEmbedder:
    """Hermetic embedder for daemon integration tests."""

    dimension: int = 16
    model_name: str = "BAAI/bge-small-en-v1.5"

    def embed(self, text: str) -> list[float]:
        del text
        v = [0.0] * self.dimension
        v[0] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)

    def warmup(self) -> None:
        pass

    def embed_batch(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def build_runtime(vault: Path) -> ServeRuntime:
    """Construct a hermetic ServeRuntime for a vault directory."""
    embedder = FakeEmbedder()
    storage = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        embedding_dim=embedder.dimension,
        embedding_model_name=embedder.model_name,
        vault_name="testvault",
    )
    vault_lock = VaultLock(vault, install_signal_handlers=False)
    vault_lock.acquire()
    fastmcp_server = build_server(
        storage,
        embedder,
        default_user="testuser",
        server_name="engram",
    )
    config = EffectiveConfig(
        default_user="testuser",
        vault_path=vault,
        thoughts_dir=vault / "thoughts",
        index_dir=vault / ".indexes",
        embedding_model=embedder.model_name,
        vault_name="testvault",
        sync=SyncConfig(disabled=True, auto_pull_on_startup=False),
        llm=LLMConfig(),
        aggregator=AggregatorConfig(),
        daemon=DaemonConfig(),
    )
    return ServeRuntime(
        config=config,
        vault_lock=vault_lock,
        storage=storage,
        coordinator=None,
        embedder=embedder,
        fastmcp_server=fastmcp_server,
    )


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """A short-path vault under ``/tmp`` for UDS-path-limit safety."""
    with tempfile.TemporaryDirectory(prefix="eng-int-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        (vault / "thoughts").mkdir(parents=True)
        (vault / ".indexes").mkdir(parents=True)
        yield vault


@pytest.fixture
async def running_daemon(short_vault: Path) -> AsyncIterator[tuple[DaemonServer, SocketPaths]]:
    """Boot a DaemonServer on a short-path vault; yield (daemon, paths)."""
    runtime = build_runtime(short_vault)
    daemon = DaemonServer(
        runtime=runtime,
        daemon_config=DaemonConfig(idle_shutdown_seconds=0),
    )
    paths = resolve_paths(short_vault)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=10.0)
    try:
        yield daemon, paths
    finally:
        daemon.request_shutdown()
        await asyncio.wait_for(server_task, timeout=10.0)
