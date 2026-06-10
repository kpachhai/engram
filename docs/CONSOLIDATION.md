# Consolidation (`engram consolidate`)

As a vault grows, semantic retrieval degrades: near-duplicate captures crowd
the top-k slots, stale notes surface beside current ones, and contradictory
thoughts coexist silently. `engram consolidate` is the curation pass that
counters this - report first, mutate only on explicit apply.

The architecture splits cleanly along engram's storage thesis:

- the **episodic record stays complete**: superseded originals are *moved*
  to `<vault>/archive/` with their bodies byte-untouched, git-tracked
  forever;
- only the **semantic index is curated**: the originals' rows leave SQLite,
  so retrieval stops surfacing them; the merged distillation takes their
  place.

## Report mode (default)

```bash
engram consolidate                 # full run (uses your configured LLM)
engram consolidate --no-llm        # similarity passes only
engram consolidate --prefix Lesson # scope to one prefix
```

Report mode never mutates the vault. It opens the index read-only (safe to
run while the daemon is up) and runs four passes:

| Pass | Method | Output |
|---|---|---|
| Exact duplicates | content fingerprint | keep-newest proposals |
| Near-duplicates | full pairwise embedding similarity, within-prefix, complete-linkage partition | merge proposals with an LLM-distilled draft |
| Stale candidates | age-only (`created_at`/`updated_at`/`legacy_created_at` anchors) | report-only list |
| Contradictions | similarity band below the near-dup threshold, LLM-judged | report-only list |

The report lands at `<vault>/.indexes/consolidate/report-<utc>.json`
(per-machine state, never synced). Every proposal pins its targets to
`(thought_id, fingerprint)`.

Honesty rules: passes interrupted by a provider failure or the daily cost
cap are marked `incomplete after N of M`, never presented as clean; pending
or failed embeddings are counted and surfaced (run `engram doctor --repair`
first for full coverage); clusters containing a `portability=block` member
are never sent to an LLM and surface as manual-review instead.

Note: report mode is zero-VAULT-mutation, not zero-egress - LLM passes send
portable thought content to your resolved provider and record budget usage.
The portability gates (`block` never; `sensitive` local-only) apply
unchanged.

## Defaults and tuning

Two defaults encode evidence from the first real-vault run
(bge-small-en-v1.5, ~600 thoughts):

- **`--threshold 0.93`** (near-duplicate similarity). True duplicates on
  that corpus scored 0.94-0.99 while pairs needing human judgment - notably
  a lesson and its deliberate correction - sat at 0.90-0.92. The
  contradiction band is `contradiction-threshold <= similarity < threshold`,
  so the judgment band flows to the LLM contradiction judge (report-only)
  instead of merge proposals. Lower `--threshold` toward 0.90 for
  recall-first reports; the apply gate still protects the vault.
- **`--exclude-prefix "Session Summary"`** (repeatable). Log-like prefixes
  cluster on shared structure, not shared meaning - 5 of the 16 clusters in
  that first report were session summaries whose template similarity made
  them embedding-near-duplicates that must never merge. Excluded prefixes
  skip the similarity passes (near-dup clustering + contradiction judging)
  but still get exact-duplicate keep-newest and staleness coverage. The
  exclusion is surfaced in the report and the CLI summary, never silent.
  An explicit `--prefix X` scope overrides the exclusion list entirely.

## Apply mode

```bash
engram daemon stop
engram consolidate --apply        # newest report; typed confirmation
engram daemon start               # or let the next MCP connection auto-spawn
```

Apply executes **merge proposals only** (stale and contradiction findings
are report-only). It:

1. refuses team-write vaults, read-only vaults, and cloud-synced paths;
2. acquires the vault lock for the entire run - a daemon auto-spawn during
   the run fails cleanly instead of racing the index;
3. re-verifies every pinned fingerprint and skips proposals whose thoughts
   changed since the report (exit code 3 signals a partial apply);
4. captures the merged thought through the normal capture path with
   provenance frontmatter (`source: engram-consolidate`,
   `consolidated_from`, `consolidated_range`) so model-inferred content
   stays permanently distinguishable from user-captured thoughts;
5. archives originals to `archive/<same-relative-path>` with `archived_at`
   + `superseded_by` added to frontmatter (bodies untouched);
6. journals every step (`.indexes/consolidate/journal-*.jsonl`); an
   interrupted run resumes idempotently and `engram doctor` flags the
   leftover journal;
7. commits everything it touched as one git commit (non-git vaults skip the
   commit with a notice).

The merged thought inherits the MOST restrictive portability among its
members (block > sensitive > portable), so curation can never widen a
thought's reach.

## Multi-machine convergence

The consolidation commit syncs to your other machines like any capture.
After pulling it, machine B's index still holds rows for the moved files;
searches degrade until reconciliation. `engram doctor` surfaces this as the
existing orphan-rows check; fix with:

```bash
engram doctor --repair --remove-orphans
```

Automatic reconciliation at serve startup is future work.

## What consolidation is NOT

- **Not deletion.** Archived content lives on in `archive/` AND in git
  history. `engram delete` cannot target archived files once their index
  rows are gone - a PII-removal request against archived content requires a
  git-history scrub, not consolidate.
- **Not automatic.** Nothing auto-archives on age or an LLM verdict; apply
  acts only on merge proposals you have had the chance to review, behind a
  typed confirmation.
- **Not cross-vault.** Each run scopes to exactly one writable vault;
  thoughts never cluster across vault boundaries.

## Limits worth knowing

- The embedding model (`BAAI/bge-small-en-v1.5`) is English-tuned;
  similarity quality drops for non-English content (both missed duplicates
  and spurious clusters).
- Similarity passes refuse when the index was embedded under a different
  model than configured (`engram reindex --full` first) and on vaults
  beyond 20k embedded thoughts (scope with `--prefix`).
- Staleness is age-only: engram records no retrieval/access data, and an
  honest coarse signal beats an invented precise one.

Design record: [`adr/009-consolidation.md`](adr/009-consolidation.md).
