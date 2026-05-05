# ADR 001 - Storage recipe: markdown source-of-truth + SQLite + sqlite-vec

## Status

Accepted (Phase 1).

## Context

engram needs a personal-scale memory store that is:

* **Sovereign** - no hosted database, no vendor.
* **Portable** - the data must outlive the tool.
* **Diff-friendly** - users will want to read, edit, and (eventually) git-push their thoughts.
* **Fast for vector search** - <100ms p95 over 10K thoughts on a laptop.
* **Cheap to run** - one process, one user, no server tier.

A single SQLite file would be opaque to git/Obsidian/grep. A pure markdown corpus would be slow for ANN search.

## Decision

Two-layer storage:

1. **Source of truth: plain markdown files** at `<vault>/thoughts/<prefix>/...`. Each file has YAML frontmatter (id, prefix, portability, source, created_at, updated_at, fingerprint, tags) and a body. Filenames embed the last 12 hex chars of the UUID-v7 for collision safety while staying human-readable.
2. **Performance index: SQLite + sqlite-vec** at `<vault>/.indexes/engram.db`. The `thoughts` table mirrors the markdown frontmatter; the `thought_embeddings` virtual table stores 384-dim BAAI/bge-small-en-v1.5 vectors via the vec0 module.

Markdown is authoritative. The index is rebuildable. On `engram serve` startup, the storage layer walks the markdown tree and reconciles drift (fingerprint mismatch -> re-embed; new file -> insert; missing markdown -> orphan).

## Consequences

### Positive

* Users can edit thoughts in any text editor; engram picks up the change on next start (or `engram reindex`).
* Git is the natural sync transport (Phase 2+) - no custom replication protocol.
* `grep` over `<vault>/thoughts/` works as a fallback when the index is corrupted.
* A user who uninstalls engram still owns every byte of their data in plain text.
* SQLite + sqlite-vec is mature, embedded, and zero-ops.

### Negative

* Two write paths must stay in sync. Mitigated by the Flow A atomicity contract: markdown write succeeds first, SQLite txn wraps the row + embedding, embedding failure is recoverable via `engram reindex --repair`.
* Reindex on cold start has a steady-state cost (~5s for 10K thoughts per the spec NFR1 budget). Mitigated by fingerprint-based incremental reindex - only drifted files are re-embedded.
* Storage size is roughly 2x of markdown alone (markdown + SQLite index), well within the <50MB-for-10K target.

## Alternatives considered

* **Pure SQLite (with markdown as a `BLOB` column)** - rejected. Defeats the "edit in any editor" property; turns engram into a hosted-DB-shaped tool again.
* **Markdown + LanceDB / Qdrant / Chroma** - rejected. Each adds a separate process or large dependency. SQLite + sqlite-vec stays in-process and meets NFR1 with margin (37ms p95 vs 100ms target on the local 10K bench).
* **Markdown + flat-file index (e.g. JSONL)** - rejected. Cannot run vector search at the NFR1 latency target; would need to load every embedding into memory per query.

## References

* `docs/superpowers/specs/2026-05-04-engram/02-TECHNICAL_DESIGN.md` Storage Schema
* `docs/superpowers/specs/2026-05-04-engram/01-PRODUCT_SPEC.md` NFR1
* `bench/search_10k.py` - measurement harness
