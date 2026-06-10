# ADR 009 - Consolidation (report-then-action vault curation)

**Status**: Accepted
**Date**: 2026-06-09
**Phase**: 6
**Supersedes**: none
**Superseded-by**: none

## Context

engram's architecture is episodic + semantic: markdown files are the
exact episodic record, the SQLite/sqlite-vec index is the semantic
retrieval layer. Memory-interference research on semantic retrieval
predicts that any meaning-based store degrades as it grows -
near-duplicates crowd retrieval slots, stale entries surface beside
current ones, contradictory captures coexist silently. The principled
mitigation is a third leg: interference-aware consolidation that
curates the semantic layer while preserving the episodic record.

engram had storage, sync, MCP, daemon, and multivault - but no
consolidation, dedup, or decay. `engram consolidate` adds it as a
report-then-action CLI subcommand: detection passes produce a
reviewable report; `--apply` executes merge proposals only after
explicit confirmation.

Constraints that shaped the design:

1. **Pinned invariant 1 (markdown SoT, SQLite regenerable).** Any
   curation expressed only in SQLite is undone by the next
   `engram reindex`, which re-captures every `.md` under
   `thoughts_dir` missing from the index. Curation must therefore be
   a markdown-visible event.
2. **Pinned invariants 2-3 (portability gates).** Contradiction
   judging and merge distillation use an LLM; `portability=block`
   content must never reach one, and `sensitive` content only local
   providers.
3. **The daemon owns the vault.** A second SQLite writer while the
   daemon holds the vault has empirically wedged the daemon's
   connection into ``disk I/O error`` and silently dropped in-flight
   capture rows (see ``docs/DAEMON_WRITE_RESILIENCE_INVESTIGATION.md``).
   The one-shot advisory marker check used by ``delete``/``reindex``
   leaves a TOCTOU window that is unacceptable for a long-running
   write command: any MCP client connecting mid-run auto-spawns the
   daemon. The full-acquisition precedent is ``engram sync``'s
   compact path, which takes the ``VaultLock`` itself.

## Decision

1. **Archive = relocation, not deletion and not in-place flagging.**
   `--apply` moves each superseded original from
   `thoughts/<rel-path>` to `archive/<rel-path>` inside the vault.
   The body bytes are untouched (property-tested); frontmatter gains
   additive `archived_at` + `superseded_by` fields. The archive stays
   git-tracked, so the episodic record remains complete and synced;
   being outside `thoughts_dir`, it is invisible to reindex, doctor
   markdown scans, and capture - the index stays curated.
2. **Merged thoughts are first-class, provenance-marked captures.**
   The distilled thought is written through the storage facade
   (validation, slug, 1MB cap) with `source='engram-consolidate'`,
   `consolidated_from: [ids]`, and `consolidated_range` recording the
   source date span. Model-inferred content is therefore permanently
   distinguishable from user-captured thoughts. All four new
   frontmatter fields join the known-fields set so consolidated
   vaults stay drift-clean under `engram doctor`.
3. **Portability is inherited most-restrictively.** A merged thought
   takes the most restrictive portability among its members
   (block > sensitive > portable). Clusters containing a block member
   are never LLM-distilled; they surface as manual-review proposals.
   Contradiction pairs are filtered for block before any provider
   resolution; every LLM call routes through `resolve_provider`.
4. **Concurrency model: daemon-stopped one-shot.** `--apply` acquires
   the vault's `VaultLock` for its entire run and refuses (exit 2)
   when the daemon holds it - the operator runs `engram daemon stop`
   first. There is deliberately no `--force`. While consolidate holds
   the lock, a daemon auto-spawn fails cleanly instead of wedging the
   WAL. Report mode takes no lock; it opens SQLite read-only
   (URI `mode=ro`). Routing apply through the daemon is deferred.
5. **Apply is journaled and idempotent per cluster.** Order per
   cluster: journal intent -> capture merged thought (embedding
   computed eagerly; `on_index_failure='fail'`) -> archive originals
   -> delete original index rows in one transaction. Every proposal
   is pinned to `(thought_id, fingerprint)` at report time and
   re-verified at apply time; mismatches skip that proposal. A crash
   leaves the vault consistent after any prefix of clusters; re-runs
   resume from the journal. One git commit captures all touched paths
   under the lock; non-git vaults skip the commit with a notice.
6. **Detection scope is honest about its limits.** Staleness is
   age-only and report-only (engram records no retrieval/access data;
   inventing a signal would be false precision). Contradiction
   verdicts are report-only (resolution requires human judgment).
   `--apply` executes merge proposals exclusively. Reports record the
   embedding model name and refuse to run similarity passes against a
   mismatched index; pending/failed-embedding exclusions are counted
   and surfaced, and LLM passes interrupted by provider failure or
   budget caps are marked `incomplete after N of M`, never presented
   as clean results.
7. **Per-machine state lives under `.indexes/consolidate/`.** Reports
   and journals are machine-local operational state, placed under the
   already-gitignored `.indexes/` tree - never in the (mostly
   git-tracked) `.engram/` directory.
8. **Team-write vaults refuse `--apply` this phase.** Merging other
   authors' thoughts breaks `captured_by` attribution, and the team
   pre-receive hook would reject relocated blobs whose `captured_by`
   differs from the pusher. Client-side refusal plus the existing
   server-side hook keep the boundary two-layer. Report mode is
   allowed.
9. **Cloud-synced vault paths refuse `--apply`.** New, deliberately
   stricter than `engram serve` (which only warns): flock is
   unreliable on NFS/SMB/Dropbox/iCloud, and a write pass whose lock
   silently protects nothing is worse than a refusal.
10. **numpy becomes a direct dependency.** Clustering needs a full
    pairwise similarity pass (single-query KNN truncates clusters);
    numpy is already present transitively via fastembed/onnxruntime,
    so the promotion costs nothing at install time. Clustering itself
    is hand-rolled greedy highest-similarity-first partitioning -
    no scipy.

## Consequences

- A consolidated vault converges across machines through ordinary git
  sync: the consolidation commit carries the moves; the other
  machine's index shows orphan rows until
  `engram doctor --repair --remove-orphans` runs (documented in
  `docs/CONSOLIDATION.md`). Automatic reconciliation at serve startup
  is future work.
- Archived content is preserved forever - in `archive/` and in git
  history. Consolidation is curation, NOT deletion: `engram delete`
  cannot target archived files once their index rows are gone. A
  PII-removal request against archived content requires the
  history-scrub path. This limitation is documented loudly.
- Stale and contradiction findings accumulate value only if the
  operator reviews reports; nothing auto-archives on age or verdict.
- The daemon-stop requirement makes apply a deliberate maintenance
  action rather than a background behavior - acceptable for a
  curation pass expected to run on the order of weeks, not minutes.
- Restore/unarchive, team-vault apply, retrieval telemetry, and
  daemon-RPC apply are explicitly deferred future work.
