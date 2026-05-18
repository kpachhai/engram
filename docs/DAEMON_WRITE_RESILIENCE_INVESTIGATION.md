# Investigation: Silent SQLite write failures during capture

**Status:** Open. Investigation complete; fix not yet implemented.
**Filed:** 2026-05-18
**Severity:** Operational data-quality bug. No data loss (markdown SoT held).
The bug is "operator gets no signal at write time."

---

## Symptom observed

During the `engram delete` smoke-test on 2026-05-16, a freshly captured
thought reported a successful id + file path via the MCP `capture_thought`
tool, but the `engram delete <id>` CLI replied "not found." A direct
SQLite query confirmed the row was not in the `thoughts` table. The
markdown file WAS on disk. Running `engram reindex` walked the markdown
tree and recovered **38 orphan thoughts** that had been silently lost in
the same way over the prior days.

## Root cause (now characterized)

The daemon's `engram.log` shows a stack-trace pattern firing on every
capture between 2026-05-13 and 2026-05-16:

```
WARNING engram.mcp.tools: capture_thought: embedding failed; capturing
        as pending. error=[ONNXRuntimeError] : 3 : NO_SUCHFILE ...
        model_optimized.onnx ... File doesn't exist
ERROR engram.storage.facade: SQLite insert failed for capture <uuid>;
      markdown SoT preserved at <path>;
      run `engram doctor --repair` to reconcile
Traceback (most recent call last):
  File ".../storage/facade.py", line 326, in capture
    _q_insert_thought(...)
  File ".../storage/sqlite_queries.py", line 121, in insert_thought
    conn.execute(...)
sqlite3.OperationalError: disk I/O error
```

Two failure modes coincide:

1. **FastEmbed ONNX model file is missing** in `/var/folders/.../fastembed_cache/`.
   macOS periodically purges `/var/folders/`. Embedding fails -> the
   thought is captured with `embedding_status='pending'` (working as
   designed; markdown still written). This is what commit `e087a19`
   (the doctor broken-partial FastEmbed cache check) addresses on the
   detection side.

2. **SQLite `INSERT INTO thoughts(...)` raises `disk I/O error`**
   (SQLITE_IOERR). The capture path catches `sqlite3.Error`, logs
   "SQLite insert failed; markdown SoT preserved; run doctor --repair
   to reconcile," and returns the `Thought` as if the capture
   succeeded. The MCP response shows success.

The capture/markdown pairing is correct (markdown SoT is preserved); the
operator-signaling is the gap.

### Why SQLite raised `disk I/O error`

Working hypothesis, not yet confirmed by a reproduction:

- The disk was at **94% capacity** (846 GB used of 932 GB; APFS).
  APFS gets unhappy past ~90% and starts returning sporadic EIO on
  allocations.
- The WAL had grown to ~3.7 MB without checkpointing back to the main
  DB. (Manually running `PRAGMA wal_checkpoint(TRUNCATE)` shrank it to
  zero with no busy / no-frames-recovered result, suggesting the WAL
  was a ghost-allocation rather than holding committed-but-unmerged
  state.)
- The FastEmbed ONNX failures correlate temporally but are not causally
  upstream of SQLite; they share the underlying disk-pressure trigger.

Captures since 2026-05-18 (after a daemon restart + maybe a manual
`engram doctor --download-model`) are NOT failing. The condition
appears to come and go with disk pressure / FastEmbed cache state.

## The actual bug

`src/engram/storage/facade.py` ~ line 350:

```python
try:
    _q_insert_thought(self.conn, ...)
except sqlite3.Error:
    # Markdown is on disk (SoT); SQLite is out of sync. Doctor will reconcile.
    # Per Flow A step 3 commentary: log and continue; capture still succeeds.
    _log.exception(
        "SQLite insert failed for capture %s; markdown SoT preserved at %s; "
        "run `engram doctor --repair` to reconcile",
        thought.id,
        absolute_path,
    )
```

The intent is good (markdown SoT > SQLite > die-on-failure). The
implementation has two problems:

1. **The MCP response says the capture succeeded.** Search /
   list / fetch on the returned id will all fail (the row isn't in the
   index). The AI assistant on the other side of MCP has no way to
   know.
2. **No operator-visible counter increments.** `engram doctor` and
   `engram thought_stats` don't surface "N captures wrote markdown
   only" anywhere. The only signal is the daemon log file, which
   nobody is tailing.

## Recovery (today, manual)

```bash
engram daemon stop
engram reindex            # incremental: walks markdown, inserts missing SQLite rows
engram daemon start
```

This worked on the observed incident (38 rows recovered).

## Proposed fixes (pick one or both)

### Option A: Surface the failure in the MCP response

Change `VaultStorage.capture` to return a tuple `(Thought, IndexState)`
where `IndexState ∈ {ok, pending, failed}`. The MCP `capture_thought`
tool maps `failed` -> `"index_state": "failed"` in the response (still
returns id + file_path so the markdown is reachable). AI clients can
detect degraded state and surface to the user; programmatic callers
can branch.

Cost: minor signature change, additive to the MCP wire format
(unbreaking per pinned invariant 6). Two existing callers to update
(`storage.facade::capture` and `mcp.tools::capture_thought_handler`).

### Option B: Add an `engram doctor` orphan-markdown check

`engram doctor` already walks SQLite-side orphans (rows referencing
markdown files that no longer exist). Add a check for the inverse:
markdown files on disk with no SQLite row. Emit WARN with the count.

Cost: new check in `engram.diagnostics.doctor`. The walk is O(N) over
markdown files; SQLite-row-lookup is O(1) per file via `get_by_id`.
For a 400-thought vault this is sub-second; for 100k it's a few
seconds. Gate behind `--deep-orphan-scan` if needed.

### Option C (out of scope for this followup)

Address the *underlying* SQLite EIO. Likely involves:
- A `PRAGMA busy_timeout` to ride out transient EIO.
- `engram doctor` flagging disk usage > 90% as a WARN before the
  cascade.
- Retry-with-backoff inside `_q_insert_thought` on `OperationalError`.

This is a bigger change; should be filed separately if Option A/B
don't suffice.

## Recommendation

**Option A first, Option B as a follow-up.** Option A is the smaller
change with bigger operator-experience win — the AI client gets a
real-time signal instead of silently writing into a degraded index.
Option B is good preventative health monitoring but doesn't address
the in-the-moment write.

Either way: the `engram delete` / `engram reindex` lock-awareness
change shipped in `019c583` already eliminates one CLI-induced trigger
of this same code path, so the bug should be observed less frequently
in practice.

## Reproduction recipe (for future fix-validation)

Not yet validated end-to-end; sketched here so the fix can be
verified.

```bash
# 1. Construct a vault where SQLite can be made to throw a transient
#    sqlite3.OperationalError on INSERT. Easiest path: mock
#    `_q_insert_thought` to raise once.
# 2. Capture via MCP capture_thought.
# 3. Assert: MCP response carries some signal of failure (Option A) OR
#    engram doctor surfaces an orphan-markdown warn (Option B).
# 4. Confirm markdown file is still on disk; SQLite row is absent.
# 5. Run engram reindex; assert row is recovered.
```

## Cross-reference

- Plan + first attempt at delete-CLI test: `docs/DELETE_THOUGHT_PLAN.md`
- FastEmbed broken-cache doctor check (already shipped): commit
  `e087a19`
- CLI vault-lock refusal (just shipped): commit `019c583`
