"""FastEmbed-backed embedding provider.

Lazy-loads the model on first use so ``engram serve`` can satisfy the NFR1
2-second cold start (the model load itself takes 2-3 seconds; the spec
explicitly excludes it from the cold-start budget). Subsequent calls hit
the warm path (~50-100ms per query on CPU).

Async wrapper uses ``asyncio.to_thread`` because FastEmbed's ``embed`` is
synchronous (numpy + onnxruntime); calling it directly from an async
handler would block the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from engram.embedding.model_hashes import KNOWN_MODEL_HASHES

_log = logging.getLogger("engram.embedding.fastembed")

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
DEFAULT_DIMENSION = 384


def _sha256_of_file(path: Path) -> str:
    """Stream-hash a file in 64 KiB chunks; returns lowercase hex digest."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


class FastEmbedProvider:
    """Lazy-loaded FastEmbed wrapper.

    Construction is fast (no model download); the model is loaded on the
    first :meth:`embed` call. Subsequent calls are warm-path (~50-100ms on CPU).
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        dimension: int = DEFAULT_DIMENSION,
        cache_dir: Path | None = None,
    ) -> None:
        """Construct the provider; model load is deferred until first embed."""
        self._model_name = model_name
        self._dimension = dimension
        self._cache_dir = cache_dir
        self._model: Any = None
        self._load_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        """The configured FastEmbed model identifier."""
        return self._model_name

    @property
    def dimension(self) -> int:
        """Configured vector dimension; verified against the loaded model on first use."""
        return self._dimension

    @property
    def is_loaded(self) -> bool:
        """``True`` once the model has been lazily loaded into memory."""
        return self._model is not None

    def _load(self) -> None:
        """Load the FastEmbed model. Verifies hashes if a manifest is populated."""
        with self._load_lock:
            if self._model is not None:
                return
            from fastembed import TextEmbedding

            self.verify_model_files()

            kwargs: dict[str, Any] = {}
            if self._cache_dir is not None:
                kwargs["cache_dir"] = str(self._cache_dir)
            self._model = TextEmbedding(model_name=self._model_name, **kwargs)
            self._verify_dimension_after_load()

    def _verify_dimension_after_load(self) -> None:
        """Sanity-check that the loaded model emits ``self._dimension``-vectors."""
        # Cheap probe: embed a single short string and check the vector length.
        assert self._model is not None  # noqa: S101 - post-load invariant
        sample_iter: Iterable[Any] = self._model.embed(["dimension probe"])
        sample = next(iter(sample_iter))
        actual_dim = len(sample)
        if actual_dim != self._dimension:
            from engram.errors import EmbeddingError

            msg = (
                f"FastEmbed model {self._model_name!r} produced {actual_dim}-dim vectors; "
                f"engram is configured for {self._dimension}. Update the configured "
                f"`embedding_model` or run `engram reindex --full`."
            )
            raise EmbeddingError(msg)

    def verify_model_files(self) -> None:
        """Verify each cached model file matches its pinned SHA-256 hash.

        When the hash manifests in :mod:`engram.embedding.model_hashes` are
        empty placeholders, this method logs a single WARNING and returns.

        FastEmbed downloads models into the HuggingFace cache layout
        (``<cache>/models--<org>--<repo>/snapshots/<commit>/<file>``).
        We resolve the most-recent snapshot directory and verify each
        manifest entry against the file at that path.
        """
        manifest = KNOWN_MODEL_HASHES.get(self._model_name)
        if not manifest:
            _log.warning(
                "model %s has no pinned SHA-256 manifest; skipping hash verification "
                "(populate engram/embedding/model_hashes.py to enable verification)",
                self._model_name,
            )
            return

        if self._cache_dir is None or not self._cache_dir.exists():
            _log.warning(
                "model cache dir %s missing at verify time; deferring to FastEmbed",
                self._cache_dir,
            )
            return

        snapshot_dir = self._resolve_snapshot_dir()
        if snapshot_dir is None:
            _log.warning(
                "no model snapshot found under %s; FastEmbed will download on next call",
                self._cache_dir,
            )
            return

        for filename, expected_hash in manifest.items():
            file_path = snapshot_dir / filename
            if not file_path.exists():
                _log.warning(
                    "model file %s missing in %s; FastEmbed will download",
                    filename,
                    snapshot_dir,
                )
                continue
            # Resolve symlinks (HF stores blobs separately + symlinks them).
            real = file_path.resolve()
            actual = _sha256_of_file(real)
            if actual != expected_hash:
                from engram.errors import EmbeddingError

                msg = (
                    f"model integrity check failed: {filename} hash {actual!r} != "
                    f"pinned {expected_hash!r}. Refusing to load. Verify HuggingFace "
                    f"download or pre-stage from a trusted source."
                )
                raise EmbeddingError(msg)

    def _resolve_snapshot_dir(self) -> Path | None:
        """Locate the snapshot directory under the HuggingFace cache layout.

        Returns the most-recently-modified snapshot directory, or None if
        no snapshots exist under the cache. The HF layout is
        ``<cache>/models--<org>--<repo>/snapshots/<commit>/``.
        """
        if self._cache_dir is None:
            return None
        snapshots: list[Path] = []
        for repo_dir in self._cache_dir.glob("models--*"):
            snap_root = repo_dir / "snapshots"
            if snap_root.exists():
                snapshots.extend(d for d in snap_root.iterdir() if d.is_dir())
        if not snapshots:
            return None
        return max(snapshots, key=lambda d: d.stat().st_mtime)

    def list_cached_files(self) -> dict[str, Path]:
        """Return ``{filename: path}`` for the currently cached model files.

        Used by ``engram doctor --print-hashes`` to compute and print the
        hashes of the cached files in a manifest-ready format. Returns an
        empty dict when no cache snapshot exists.
        """
        snapshot = self._resolve_snapshot_dir()
        if snapshot is None:
            return {}
        return {f.name: f for f in snapshot.iterdir() if f.is_file() or f.is_symlink()}

    def embed(self, text: str) -> list[float]:
        """Embed a single string; loads the model lazily on first call."""
        if self._model is None:
            self._load()
        assert self._model is not None  # noqa: S101 - post-load invariant
        result_iter: Iterable[Any] = self._model.embed([text])
        first_vector = next(iter(result_iter))
        return [float(x) for x in first_vector]

    async def aembed(self, text: str) -> list[float]:
        """Async wrapper around :meth:`embed`; uses ``asyncio.to_thread``."""
        return await asyncio.to_thread(self.embed, text)


__all__ = ["DEFAULT_DIMENSION", "DEFAULT_MODEL_NAME", "FastEmbedProvider"]
