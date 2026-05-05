"""Synthetic 100-thought reference corpus generator.

Per ``02-TECHNICAL_DESIGN.md`` (`Fixture corpus`) and ``10-CODE_QUALITY.md``
testing rules, the engram test suite needs a stable, synthetic corpus that:

* Covers every canonical prefix in :mod:`engram.models.frontmatter`.
* Spans the three portability values (``portable``, ``sensitive``, ``block``).
* Has a deterministic spread of ``created_at`` timestamps so search ordering,
  pagination, and time-window filters can be exercised without flakiness.
* Generates only synthetic strings - never real user data (per technical
  design rule "test infrastructure does not retain user data").

The :func:`build_corpus` function returns a list of
:class:`CorpusThought` records; ``write_corpus_to_vault`` captures them into
a :class:`engram.storage.facade.VaultStorage` and returns the resulting
``list[Thought]``. A 10K-thought variant exists for the benchmarks in
``bench/``; the same generator drives both via the ``size`` argument.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from engram.models.frontmatter import CANONICAL_PREFIXES, Portability

if TYPE_CHECKING:
    from engram.embedding.protocol import EmbeddingProvider
    from engram.models import Thought
    from engram.storage.facade import VaultStorage


#: Default corpus size for the reference fixture. Chosen per technical design.
DEFAULT_CORPUS_SIZE: int = 100

#: Anchor moment for ``created_at`` spread; keeping this fixed (and tz-aware)
#: makes the generator deterministic across machines and CI runs.
_EPOCH_ANCHOR: datetime = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

#: Synthetic source identifiers used in lieu of real users.
_SYNTHETIC_SOURCES: tuple[str, ...] = (
    "fixture-user-a",
    "fixture-user-b",
    "fixture-user-c",
)

#: Topic vocabulary for the synthetic body sentences. The corpus generator
#: composes thoughts from these tokens; semantic relevance for search-ranking
#: tests comes from co-occurrence rather than realistic prose.
_TOPIC_VOCAB: tuple[str, ...] = (
    "embedding",
    "fingerprint",
    "vault",
    "sqlite",
    "markdown",
    "yaml",
    "atomic write",
    "fsync",
    "uuid7",
    "open brain",
    "migration",
    "portability",
    "sync coordinator",
    "schema drift",
    "doctor check",
    "reindex",
    "MCP tool",
    "stdio transport",
    "fastembed",
    "vec0 ANN",
)

#: Tag pool. Tags are sampled (with replacement-free choice) per thought.
_TAG_POOL: tuple[str, ...] = (
    "phase-1",
    "phase-2",
    "byoc",
    "design",
    "ops",
    "security",
    "docs",
    "ci",
    "perf",
    "research",
)


@dataclass(frozen=True, slots=True)
class CorpusThought:
    """One synthetic thought, ready to be passed into ``VaultStorage.capture``.

    The fields mirror :func:`engram.storage.facade.VaultStorage.capture` so
    test code can spread the dataclass directly into ``capture(**...)``.
    """

    content: str
    prefix: str
    portability: Portability
    source: str
    tags: tuple[str, ...]
    created_at: datetime


def _derive_seed(label: str, *, salt: int) -> int:
    """Derive a stable 64-bit seed for the given label/salt.

    Using ``random.seed(label)`` directly is reproducible but couples too
    tightly to Python's hashing implementation; an explicit BLAKE2 digest is
    portable across runtimes.
    """
    digest = hashlib.blake2b(f"{label}:{salt}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


def _portability_for(prefix: str, rng: random.Random) -> Portability:
    """Pick a portability value with a realistic skew per prefix."""
    if prefix in {"Domain", "Artifact"}:
        return rng.choices(
            ("sensitive", "portable", "block"),
            weights=(0.7, 0.2, 0.1),
            k=1,
        )[0]
    return rng.choices(
        ("portable", "sensitive", "block"),
        weights=(0.85, 0.1, 0.05),
        k=1,
    )[0]


def _body_for(prefix: str, idx: int, rng: random.Random) -> str:
    """Compose a synthetic body string for the given prefix.

    The body always starts with ``[<Prefix>]`` so the prefix-detector parser
    extracts it verbatim during reindex round-trip tests.
    """
    nouns = rng.sample(_TOPIC_VOCAB, 3)
    sentences = (
        f"[{prefix}] Synthetic fixture #{idx} about {nouns[0]} and {nouns[1]}.",
        f"This corpus entry exercises the {nouns[0]} <-> {nouns[2]} interaction "
        "under storage-layer round-trips.",
        f"Tagged for portability checks; engram fixture only ({prefix}).",
    )
    return "\n\n".join(sentences)


def build_corpus(
    *,
    size: int = DEFAULT_CORPUS_SIZE,
    seed_label: str = "engram-fixture-v1",
) -> list[CorpusThought]:
    """Generate ``size`` synthetic :class:`CorpusThought` records.

    The corpus is deterministic given ``size`` and ``seed_label``. Coverage
    invariants (verified by ``test_corpus.py``):

    * Every canonical prefix appears at least once when ``size >= 30``.
    * Every portability value appears at least once when ``size >= 30``.
    * ``created_at`` is strictly increasing across the returned list.
    """
    rng = random.Random(_derive_seed(seed_label, salt=size))  # noqa: S311
    thoughts: list[CorpusThought] = []
    base = _EPOCH_ANCHOR

    for idx in range(size):
        prefix = CANONICAL_PREFIXES[idx % len(CANONICAL_PREFIXES)]
        portability = _portability_for(prefix, rng)
        source = rng.choice(_SYNTHETIC_SOURCES)
        tag_count = rng.randint(0, 3)
        tags = tuple(rng.sample(_TAG_POOL, tag_count)) if tag_count else ()
        created_at = base + timedelta(minutes=idx * 7 + rng.randint(0, 4))
        thoughts.append(
            CorpusThought(
                content=_body_for(prefix, idx, rng),
                prefix=prefix,
                portability=portability,
                source=source,
                tags=tags,
                created_at=created_at,
            )
        )

    return thoughts


def write_corpus_to_vault(
    storage: VaultStorage,
    corpus: Sequence[CorpusThought],
    *,
    embedder: EmbeddingProvider | None = None,
) -> list[Thought]:
    """Capture each :class:`CorpusThought` into ``storage`` in order.

    If ``embedder`` is provided, embeddings land alongside each row in the
    same Flow A transaction. If omitted, rows are inserted with
    ``embedding_status='pending'`` and a later ``doctor --repair`` (or
    explicit reindex) is required.
    """
    captured: list[Thought] = []
    for entry in corpus:
        embedding = embedder.embed(entry.content) if embedder is not None else None
        thought = storage.capture(
            content=entry.content,
            prefix=entry.prefix,
            portability=entry.portability,
            source=entry.source,
            tags=list(entry.tags),
            embedding=embedding,
            created_at=entry.created_at,
        )
        captured.append(thought)
    return captured


__all__ = [
    "DEFAULT_CORPUS_SIZE",
    "CorpusThought",
    "build_corpus",
    "write_corpus_to_vault",
]
