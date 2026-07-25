# ADR 004 - Embedding model: BAAI/bge-small-en-v1.5 via FastEmbed

## Status

Accepted (Phase 1).

## Context

engram needs an embedding model for semantic search. The selection space is large (OpenAI text-embedding-3, Cohere embed-v3, Voyage, Jina, BGE family, MiniLM, GTE, ...). The right answer depends on what we are optimizing for at engram's scale (single user, ~10K-100K thoughts).

Constraints derived from the spec:

* **Local-only inference.** NFR3 (privacy/sovereignty) forbids any network egress for embeddings. This eliminates every hosted API.
* **Cold-start budget under 2 seconds.** NFR1 says `engram serve` must respond to MCP `initialize` in <2s. The embedding model load can be deferred (lazy-loaded on first capture/search), but the package must install cleanly.
* **Footprint under 200MB total package.** NFR2 caps the installed package size. The embedding model alone gets ~130MB of that budget.
* **CPU-only inference.** No GPU dependency; engram runs on whatever laptop the user already has.
* **Quality sufficient for personal-scale search.** ~10K-100K thoughts; we are not building a web-scale retrieval system.

## Decision

engram ships `BAAI/bge-small-en-v1.5` as the default embedding model, accessed via the `fastembed` library (which provides a portable, ONNX-runtime-backed inference path).

* **Dimensionality:** 384.
* **Quality:** competitive on MTEB at the 100M-parameter tier; effectively at parity with text-embedding-3-small for retrieval over personal-scale corpora in our internal A/B (anecdotal).
* **Footprint:** ~130MB on-disk (well under the 200MB total-package budget).
* **Inference:** ONNX runtime, CPU-only by default. `fastembed` handles model download (pinnable to a local mirror per air-gapped install) and tokenization.
* **License:** MIT - compatible with engram's Apache-2.0.

The model is **lazy-loaded on first use** under a `threading.Lock` so cold-start MCP `initialize` stays under the 2s budget. The first `capture_thought` or `search_thoughts` after cold-start absorbs the 2-3s load cost; subsequent calls hit the in-memory model.

## Consequences

### Positive

* No network egress at runtime. NFR3 met without a runtime exception list.
* Cold start fits the 2s `initialize` budget because model load is deferred.
* 384 dimensions is small enough that 10K vectors fit in <16MB at float32 - the SQLite index size budget (NFR2: <50MB for 10K thoughts) has comfortable headroom.
* Switching models is a `engram reindex --full --model <new>` away. The `engram_settings` row records the model name so reopening with a mismatched model raises a clear error pointing at the reindex command.
* `fastembed` is a thin wrapper over ONNX runtime; the default-model trajectory is well-trodden.

### Negative

* English-only. `BAAI/bge-small-en-v1.5` is trained on English; multilingual users would want a multilingual variant (e.g. `BAAI/bge-m3`). Mitigation: the model is configurable; users can swap to a multilingual model and reindex. Default stays English to keep the install footprint small.
* ONNX runtime had a wheel-availability gap for Intel Mac in 1.21+; engram pins `onnxruntime>=1.17,<1.21` to keep Intel Mac users in scope. Once Intel Mac drops out of the support matrix, the pin can lift.
* "Quality sufficient" is a judgment call backed by the personal-scale use case. A user with millions of thoughts would push us toward a larger model and revisit the NFR1 search-latency budget.

## Alternatives considered

* **OpenAI text-embedding-3-small (hosted)** - rejected. Network egress; vendor lock-in for a piece of the stack we explicitly want to control.
* **`all-MiniLM-L6-v2` via sentence-transformers** - viable; smaller (90MB), older quality. Rejected only because BGE small ships at near-parity for retrieval at a comparable footprint.
* **`BAAI/bge-m3` (multilingual)** - rejected as default; ~2GB on disk blows the package budget. Available as a configurable swap for multilingual users.
* **`text-embedding-3-large` (hosted)** - rejected for the same reason as text-embedding-3-small, plus the 3072-dim vectors would push SQLite index size past NFR2.
* **Local LLM-derived embeddings (e.g. nomic-embed via ollama)** - viable but adds an ollama dependency on the install path. Rejected for Phase 1 to keep `pip install engram-mcp-server` self-contained; revisitable if user demand surfaces.

## References

* `docs/superpowers/specs/2026-05-04-engram/02-TECHNICAL_DESIGN.md` Embeddings
* `docs/superpowers/specs/2026-05-04-engram/01-PRODUCT_SPEC.md` NFR1, NFR2
* `src/engram/embedding/fastembed.py` - `FastEmbedProvider`
* `src/engram/embedding/protocol.py` - `EmbeddingProvider` Protocol so the storage layer never imports the concrete provider
