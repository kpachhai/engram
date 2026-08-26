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
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engram.embedding.model_hashes import KNOWN_MODEL_HASHES

_log = logging.getLogger("engram.embedding.fastembed")

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
DEFAULT_DIMENSION = 384


@dataclass(frozen=True, slots=True)
class CacheIntegrityReport:
    """Outcome of a read-only FastEmbed cache integrity check.

    Reports whether the configured model's cache snapshot is present and
    intact, without triggering a download. Consumed by the
    ``embedding_cache_integrity`` doctor check.
    """

    #: Resolved cache root (explicit or FastEmbed default); ``None`` if even
    #: the cache root cannot be located on this machine.
    cache_dir: Path | None
    #: Snapshot directory for the configured model, if one has been
    #: downloaded; ``None`` when the model has never been cached here.
    snapshot_dir: Path | None
    #: Files the manifest expects in the snapshot. Empty when the model
    #: has no pinned manifest (trust-on-first-use mode).
    expected_files: tuple[str, ...]
    #: Subset of ``expected_files`` that are either absent or broken
    #: symlinks. Empty when the snapshot is intact.
    missing_files: tuple[str, ...]
    #: ``True`` when ``expected_files`` came from a populated manifest
    #: rather than a fallback heuristic. Drives doctor's severity choice.
    manifest_populated: bool

    @property
    def has_snapshot(self) -> bool:
        """Whether a model snapshot exists at all."""
        return self.snapshot_dir is not None

    @property
    def is_intact(self) -> bool:
        """Snapshot is present AND every expected file is present."""
        return self.has_snapshot and not self.missing_files


def _sha256_of_file(path: Path) -> str:
    """Stream-hash a file in 64 KiB chunks; returns lowercase hex digest."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def default_fastembed_cache_dir() -> Path:
    """Return FastEmbed's default cache root: ``<TMPDIR>/fastembed_cache``.

    Mirrors FastEmbed's own resolution so the engram doctor check inspects
    the same directory the embedding loader would. Factored out as a
    module-level function so tests can monkeypatch this single point
    rather than the stdlib ``tempfile`` module.
    """
    return Path(tempfile.gettempdir()) / "fastembed_cache"


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
        The cache root is resolved the same way FastEmbed resolves it -
        explicit ``cache_dir`` override if given, else the module default -
        because every call site on a serving path leaves it unset. We then
        resolve the most-recent snapshot directory and verify each manifest
        entry against the file at that path.
        """
        manifest = KNOWN_MODEL_HASHES.get(self._model_name)
        if not manifest:
            _log.warning(
                "model %s has no pinned SHA-256 manifest; skipping hash verification "
                "(populate engram/embedding/model_hashes.py to enable verification)",
                self._model_name,
            )
            return

        cache_dir = self._resolve_effective_cache_dir()
        if not cache_dir.exists():
            _log.warning(
                "model cache dir %s missing at verify time; deferring to FastEmbed",
                cache_dir,
            )
            return

        snapshot_dir = self._scan_snapshots_under(cache_dir)
        if snapshot_dir is None:
            _log.warning(
                "no model snapshot found under %s; FastEmbed will download on next call",
                cache_dir,
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
        return self._scan_snapshots_under(self._cache_dir)

    def _resolve_effective_cache_dir(self) -> Path:
        """Resolve the cache root used by FastEmbed for this provider.

        Mirrors FastEmbed's own resolution: explicit ``cache_dir`` if set,
        else :func:`default_fastembed_cache_dir`. The returned path may
        not yet exist on disk; callers should check before iterating it.
        """
        if self._cache_dir is not None:
            return self._cache_dir
        return default_fastembed_cache_dir()

    @staticmethod
    def _scan_snapshots_under(cache_dir: Path) -> Path | None:
        """Find the most-recently-modified snapshot directory under ``cache_dir``."""
        if not cache_dir.exists():
            return None
        snapshots: list[Path] = []
        for repo_dir in cache_dir.glob("models--*"):
            snap_root = repo_dir / "snapshots"
            if snap_root.exists():
                snapshots.extend(d for d in snap_root.iterdir() if d.is_dir())
        if not snapshots:
            return None
        return max(snapshots, key=lambda d: d.stat().st_mtime)

    def check_cache_integrity(self) -> CacheIntegrityReport:
        """Read-only check that the configured model's cache snapshot is intact.

        Never triggers a download. Resolves the FastEmbed cache root
        (explicit override or the default ``<TMPDIR>/fastembed_cache``),
        locates the snapshot for the configured model, and verifies that
        every file listed in the pinned manifest is present and follows
        through to an existing blob.

        The doctor command surfaces a WARN row when this returns missing
        files; load-time integrity is enforced separately via
        :meth:`verify_model_files`.
        """
        effective_cache = self._resolve_effective_cache_dir()
        if not effective_cache.exists():
            return CacheIntegrityReport(
                cache_dir=None,
                snapshot_dir=None,
                expected_files=(),
                missing_files=(),
                manifest_populated=False,
            )

        snapshot_dir = self._scan_snapshots_under(effective_cache)
        manifest = KNOWN_MODEL_HASHES.get(self._model_name, {})
        manifest_populated = bool(manifest)
        expected = tuple(manifest.keys()) if manifest else ()

        if snapshot_dir is None:
            return CacheIntegrityReport(
                cache_dir=effective_cache,
                snapshot_dir=None,
                expected_files=expected,
                missing_files=(),
                manifest_populated=manifest_populated,
            )

        missing: list[str] = []
        for filename in expected:
            path = snapshot_dir / filename
            if not path.exists():
                # Catches both absent entries AND symlinks whose target blob is gone -
                # ``Path.exists`` follows symlinks and returns False on dangling ones.
                missing.append(filename)

        return CacheIntegrityReport(
            cache_dir=effective_cache,
            snapshot_dir=snapshot_dir,
            expected_files=expected,
            missing_files=tuple(missing),
            manifest_populated=manifest_populated,
        )

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


__all__ = [
    "DEFAULT_DIMENSION",
    "DEFAULT_MODEL_NAME",
    "CacheIntegrityReport",
    "FastEmbedProvider",
    "default_fastembed_cache_dir",
]
