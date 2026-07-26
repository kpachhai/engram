# engram Phase 6 — Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `engram` v0.6.0 with `engram consolidate` — a report-then-action consolidation pass over a single vault. Report mode (default) detects near-duplicate clusters, stale-thought candidates, and contradiction candidates, and emits merge proposals with zero vault mutation. `--apply` executes merge proposals only: originals are archived body-immutably under `<vault>/archive/`, and the SQLite index is curated. Stale and contradiction findings are report-only in this phase.

**Verifier:** `tests/test_consolidate_cli_smoke.py` (hermetic, spawns the installed binary) + `tests/integration/test_consolidate_flow.py` + the full suite green with coverage >= 80%. Live-test gate: daemon restart + `engram doctor` clean + report mode against a real personal vault.

**Architecture:** A new `src/engram/consolidate/` subsystem reads the vault through the existing `VaultStorage` facade and a small set of new storage primitives. Detection passes produce a `ConsolidationReport` (JSON, per-machine, under `<vault>/.indexes/consolidate/`) with every proposal pinned to `(thought_id, fingerprint)`. Apply is a journaled, per-cluster idempotent engine that holds the vault's `VaultLock` for its full duration (the `sync compact` precedent — NOT the advisory-marker pattern), archives originals out of `thoughts_dir` (so reindex cannot resurrect them), and commits the result as one git commit. LLM judgment routes exclusively through `resolve_provider` (portability gates intact).

**Tech stack additions:** numpy (promoted from transitive to direct dependency; already in every install via fastembed→onnxruntime).

**Spec source:** the maintainer's planning-repo deep-plan (3 parallel sub-agents + code-reviewer critique + one revision pass, 2026-06-09). Decisions are restated fully in ADR 009 and this plan; the plan is self-contained.

**Status:** Ready to execute.

---

## Phase Numbering Note

Per the Phase 5 precedent (CHANGELOG 0.5.0 "Roadmap renumbering"), the roadmap-only "Phase 6 — Enterprise Scaffolding" and "Phase 7 — Enterprise Polish" renumber to **Phase 7** and **Phase 8**. This Phase 6 is consolidation. Layer H records the renumber in CHANGELOG + README roadmap.

---

## Pinned-Invariant Analysis

All 7 pinned invariants from `CLAUDE.md` hold after Phase 6:

1. **Markdown is SoT** — strengthened, not weakened. Curation is expressed as a markdown-visible event (file relocation out of `thoughts_dir` + provenance frontmatter), never as an index-only edit. The archive remains git-tracked markdown; `reindex --full` still rebuilds the index correctly because archived files are outside `thoughts_dir`.
2. **`portability=block` never reaches an LLM** — enforced twice: consolidate filters block thoughts out of every LLM candidate set before provider resolution, AND `resolve_provider` still raises if one slips through.
3. **`portability=sensitive` only to LOCAL LLM providers** — unchanged; consolidate calls `resolve_provider` per batch.
4. **Two-layer enforcement at security boundaries** — `--apply` refuses team-write vaults client-side; the team pre-receive hook remains the server-side backstop (it would reject consolidation pushes anyway — that is WHY apply refuses).
5. **GPG fingerprint identity** — untouched (team apply refused this phase).
6. **MCP wire format stable** — no MCP surface changes this phase. CLI-only.
7. **Forward-compatible markdown** — new frontmatter fields (`archived_at`, `superseded_by`, `consolidated_from`, `consolidated_range`) are additive with safe defaults; older engram versions read them as tolerated extras.

---

## Phase Deliverables

| # | Deliverable | Layer(s) |
|---|---|---|
| 1 | Report models + provenance frontmatter fields (drift-clean) | A |
| 2 | Clustering / staleness / pair-band pure functions (numpy) | B |
| 3 | Storage primitives: bulk embeddings, ro-open, archive-move, batch row delete, provenance capture | C |
| 4 | Detection passes 1-4 + report writer | C |
| 5 | Journaled idempotent apply engine | C |
| 6 | Locking + refusal gates (full-run VaultLock; daemon/cloud/team/read-only refusals) | D |
| 7 | Doctor checks (orphaned journal, archive conflict markers) | E |
| 8 | `engram consolidate` CLI subcommand, fully wired | F |
| 9 | Integration + crash-injection + hermetic CLI smoke tests | G |
| 10 | ADR 009, `docs/CONSOLIDATION.md`, README/CHANGELOG/ARCHITECTURE updates | H |

---

## Current State (what Phase 5 left)

- `VaultStorage` facade is the single storage entry-point; `capture(on_index_failure=...)` exists (write-resilience work); `CaptureOutput.index_state` surfaces degraded captures.
- SQLite schema v1: `thoughts` table (id, prefix, portability, source, created_at, updated_at, fingerprint, file_path UNIQUE, tags, legacy_id, legacy_created_at, embedding_status, captured_by, ...) + vec0 `thought_embeddings(thought_id, embedding FLOAT[384])`. Single-query KNN only (`search_thoughts_by_vector`). **No retrieval/access tracking exists.**
- `VaultLock` (flock on `.indexes/engram.lock`) held by the daemon; `sync compact` is the one-shot full-acquisition precedent; `delete`/`reindex` use the weaker advisory `serve_lock_metadata()` check.
- `reindex` (incremental + `--full`) re-captures any `.md` under `thoughts_dir` missing from SQLite; orphan-row removal opt-in. `_check_orphan_sqlite_rows` + `orphan_markdown` doctor checks exist.
- LLM stack: `resolve_provider` (block/sensitive gates), `LLMBudget` (daily cost cap), `HandlerDeps` + `_build_handler_deps`, `_wrap_thought_for_prompt` anti-injection, `provider_override` test seam.
- `move-thought` precedent: moved copy keeps id/created_at via `capture(thought_id=, created_at=)`; tombstone gets fresh id.
- Frontmatter `extra="allow"` round-trips unknown fields but doctor emits `UNKNOWN_EXTRA_FIELD` drift WARNs for fields not in `_KNOWN_FRONTMATTER_FIELDS`.
- `engram init` does not `git init` (non-git vaults are legal); `serve` log-warns on cloud-sync paths.
- Daemon auto-spawns on MCP connect; acquires `VaultLock`; always-on configs exist in the wild (`idle_shutdown_seconds: 0`).

---

## Risks

| Risk | Sev | Mitigation (step) |
|---|---|---|
| Advisory-check TOCTOU: mid-run MCP connect auto-spawns daemon into WAL wedge | High | D1: real `VaultLock` acquisition held for the entire apply run; daemon spawn fails cleanly while held (D3 test) |
| Reindex resurrects curated thoughts | High | C3: archive moves files OUT of `thoughts_dir` (both doctor orphan scans + reindex walk `thoughts_dir` only) |
| Machine-B divergence: orphan index rows after consolidation commit syncs | High | H2 docs (existing `_check_orphan_sqlite_rows` remediation verbatim: `engram doctor --repair --remove-orphans`); E3 scenario test; auto-reconcile deferred to Phase 7+ |
| Portability laundering through the distilled thought | High | C7: merged portability = most restrictive of members; block-containing clusters never LLM-distilled |
| Stale pass has no data source (no retrieval tracking) | High | Scoped out: v1 staleness is age-only AND report-only; telemetry deferred (Deferred #2) |
| Crash mid-apply leaves half-merged clusters | High | C8: journal + per-cluster idempotent units + resume; G2 crash-injection tests |
| Team pre-receive hook rejects consolidation pushes | High | D2: `--apply` refuses team-write vaults this phase |
| Report-to-apply staleness (thought edited between report and apply) | Med | C8: per-proposal fingerprint re-verification; mismatch = skip + warn, exit 3 |
| LLM hallucination as memory poisoning | Med | Contradictions report-only; C7 provenance fields mark distilled thoughts model-inferred |
| Embedding model drift invalidates similarity math | Med | C5: report records model name; passes 1/3/4 refuse on mismatch with `engram_settings` |
| Dirty working tree / sync-coordinator bypass after apply | Med | C9: one explicit git commit of all touched paths under the lock; non-git vault = skip + notice |
| Multivault scope bleed (cross-vault clustering) | Med | F1: CLI operates on exactly one writable vault per run |
| LLM budget exhaustion mid-pass = misleading report | Med | C6: pre-estimate + candidate cap + per-pass `incomplete after N of M` status |
| Doctor check-code tuple coupling; conflict scan misses `archive/` | Med | E1/E2: tuples + count tests updated; scan extended |
| Archived PII persists in archive/ + git history; `delete` can't reach archived files | Low | H2: loud docs limitation + history-scrub pointer; archive-aware delete deferred |
| `file_path` UNIQUE collision on future restore | Low | Restore/unarchive out of scope (Deferred #4) |
| flock no-op on cloud-synced paths | Low | D2: apply refuses cloud-sync paths (NEW, stricter than serve's warn — stated in ADR 009) |

---

## Edge Cases

**Degenerate inputs** — empty vault / single thought: "nothing to do" report, exit 0, impossible passes explained (C5). All-identical thoughts: fingerprint pre-pass; similarity==1.0 must not be excluded by a strict-less-than guard (B1 test). All embeddings pending/failed: passes 1/3/4 skip loudly with `doctor --repair` guidance (C5). All-block vault: LLM passes skip with counts; embedding passes still run (C6/C7). All-sensitive + remote-only provider: resolver refusal surfaces in report (C6). Read-only vault: apply refused (D2).

**Cluster topology** — overlap: impossible by construction (greedy partition, B1). Transitive chains: greedy highest-similarity-first; linkage rule stated in report (B1). Oversized clusters (> `--max-cluster-size`, default 12): flagged manual-review, never auto-merged (C7). Singletons + self-matches: excluded (B1). Degenerate whole-vault cluster (>25% of vault): threshold warning, no proposal (B1). Vault > 20k thoughts: refuse with guidance (B1 guard; chunking deferred).

**Concurrency** — daemon holds vault: refuse exit 2 (D1). Capture mid-run: snapshot semantics; thoughts created/updated after snapshot are untouched (C8). Two consolidate runs: second fails flock (D1). Git pull mid-run: prevented while lock held (daemon down); manual pulls mitigated by apply-time fingerprint checks (C8).

**Errors / rollback** — LLM down mid-pass: partial-pass marking, other passes still valid (C6). Crash between archive-move and index update: journal recovery + markdown-SoT reconciliation; G2 tests. SQLite insert failure for merged thought: `on_index_failure='fail'` fails that cluster only (C8). Embedding failure for merged thought: lands `embedding_status='pending'` + report notice (C8).

**Content** — mixed-portability cluster: most-restrictive wins (C7). Cross-prefix clusters: within-prefix only this phase (B1 default; cross-prefix flag deferred). Oversized thoughts/pairs vs provider context: skip + report, never truncate into a verdict (C6); merged output re-passes the 1MB capture cap (C8). Non-English content: docs note (H2); no refusal. Back-references to archived ids: `superseded_by` frontmatter + old-id→merged-id map in report (C3/C5). Team `captured_by`: refused this phase (D2).

**Time** — age = `max(created_at, updated_at)`; `legacy_created_at` anchors migrated thoughts (B2). Future-dated thoughts (created_at > now+24h): excluded from staleness, reported as data-quality finding (B2). Clock skew: coarse default threshold (180d) makes ±hours noise (B2). All datetime math tz-aware UTC (B2). Merged thought gets fresh `created_at` + `consolidated_range` preserving source dates (C7).

---

## Plan

### Layer A — Models + errors + constants

- [ ] **A1** `src/engram/consolidate/models.py`: `ClusterProposal` (member ids + fingerprints, similarity stats, distilled draft optional, action = merge|manual-review, portability resolution), `StaleCandidate`, `ContradictionCandidate` (pair + verdict + rationale), `PassStatus` (complete | incomplete(reason, done, total) | skipped(reason)), `ConsolidationReport` (vault name, snapshot ts, embedding model, per-pass status + exclusion counts, proposals, old-id→merged-id map), `JournalEntry`. Inputs `extra="forbid"`. -> verify: `tests/consolidate/test_models.py` round-trip + rejection of unknown fields.
- [ ] **A2** `src/engram/errors.py`: `ConsolidateError` subtree (`error_code` per class): busy-vault, stale-report, model-mismatch, oversized-vault. -> verify: `tests/test_errors.py` extension asserts codes + hierarchy.
- [ ] **A3** `src/engram/models/frontmatter.py`: add `archived_at: datetime | None`, `superseded_by: str | None`, `consolidated_from: list[str] | None`, `consolidated_range: tuple[datetime, datetime] | None` to the model + `_KNOWN_FRONTMATTER_FIELDS`. -> verify: `tests/models/` drift test proves files carrying these fields emit NO `UNKNOWN_EXTRA_FIELD`; forward-compat round-trip preserved.
- [ ] **A4** `src/engram/diagnostics/check_codes.py`: new codes `consolidate_journal_orphan`, `archive_conflict_markers` (constants only; checks land in Layer E). -> verify: existing check-code tests updated counts.

### Layer B — Pure functions (no IO)

- [ ] **B1** `src/engram/consolidate/clustering.py`: `cosine_matrix(vectors)` (numpy), `greedy_partition(matrix, ids, threshold, max_cluster_size)` — highest-similarity-pair-first, each id in at most one cluster, self-matches excluded, similarity==1.0 included; `degenerate_guard(clusters, vault_size)` (>25% warning); vault-size guard (>20k refuse). numpy promoted to direct dependency in `pyproject.toml`. -> verify: `tests/consolidate/test_clustering.py` chain topology (A~B~C, A≁C), no-overlap property (hypothesis), cap enforcement, identical-vector inclusion, singleton exclusion.
- [ ] **B2** `src/engram/consolidate/staleness.py`: `effective_age(created, updated, legacy_created, now)`, future-dated guard (>24h tolerance). All tz-aware. -> verify: `tests/consolidate/test_staleness.py` incl. legacy anchor + future-dated + naive-datetime rejection.
- [ ] **B3** `src/engram/consolidate/pairs.py`: contradiction band pair generation (similarity in [contradiction_threshold, near_dup_threshold)), deterministic ordering, candidate cap. -> verify: `tests/consolidate/test_pairs.py` band boundaries.

### Layer C — Storage primitives + detection passes + apply engine

- [ ] **C1** `src/engram/storage/sqlite_queries.py`: `fetch_all_embeddings(conn) -> list[tuple[str, list[float]]]` (status='ok' only) + `delete_thought_rows(conn, ids)` (single transaction, parameterized). -> verify: `tests/storage/test_sqlite_queries.py` additions.
- [ ] **C2** `src/engram/storage/sqlite.py`: `open_connection_readonly(path)` via URI `mode=ro`; on WAL-recovery failure (`SQLITE_READONLY_CANTINIT` class) raise with remediation ("daemon exited uncleanly; run `engram doctor`"). -> verify: test with leftover `-wal` + read-only dir simulation.
- [ ] **C3** `src/engram/storage/archive.py`: `archive_thought_file(vault, rel_path, superseded_by, archived_at)` — read original, annotate frontmatter (body bytes untouched), atomic-write to `<vault>/archive/<rel_path>`, remove original; returns both paths. -> verify: property test asserts body bytes identical pre/post; frontmatter parses with A3 fields; original gone.
- [ ] **C4** `src/engram/storage/facade.py`: extend the capture path to accept additive provenance frontmatter (`consolidated_from`, `consolidated_range`) — minimal seam, additive-only. -> verify: capture round-trip test shows fields in written markdown + no drift WARN.
- [ ] **C5** `src/engram/consolidate/passes.py`: orchestration — snapshot pinning, exclusion accounting (pending/failed embeddings, block counts), pass 1 (fingerprint pre-pass + B1 clustering), pass 2 (B2 staleness, report-only), model-mismatch refusal, degenerate/empty-vault handling. `src/engram/consolidate/report.py`: writer/loader for `.indexes/consolidate/report-<utc-ts>.json`. -> verify: `tests/consolidate/test_passes.py` with synthetic vault fixtures; report JSON schema stability test.
- [ ] **C6** `src/engram/consolidate/llm.py`: contradiction judging (pass 3) — block filtering BEFORE pair building, `resolve_provider` per batch, `LLMBudget` pre-estimate + cap, structured verdict parsing, oversized-pair skip, partial-pass status. -> verify: `tests/consolidate/test_llm.py` with `provider_override` mock; budget-exhaustion test asserts `incomplete` marking; block thought never appears in any prompt (assert on mock calls).
- [ ] **C7** merge proposals (pass 4) in `llm.py`/`passes.py`: distillation prompt via `_wrap_thought_for_prompt`, most-restrictive portability computation, block/oversized/capped clusters -> manual-review (no distilled draft), `--no-llm` degrades all non-exact clusters to manual-review (exact-dup clusters keep keep-newest/archive-rest action, no LLM needed). -> verify: portability inheritance matrix test; `--no-llm` path test.
- [ ] **C8** `src/engram/consolidate/apply.py`: journaled engine. Per cluster: (1) journal intent, (2) capture merged thought — embedding computed via `FastEmbedProvider` and passed in (fallback pending + notice), `on_index_failure='fail'`, provenance fields, re-validate 1MB cap, (3) C3 archive-move originals, (4) C1 row delete transaction. Fingerprint re-verify per proposal (skip + warn on mismatch). Resume-from-journal idempotency (cluster done = merged exists + originals archived). Snapshot guard: refuse proposals touching thoughts modified after report snapshot. -> verify: `tests/consolidate/test_apply.py` unit tests per ordering step; mismatch-skip; resume.
- [ ] **C9** apply finalization: collect all touched paths -> single `gitops` commit under the lock; non-git vault: skip with notice. Exit-status synthesis: 0 all applied / 3 partial (skips or failures) — raised to CLI in Layer F. -> verify: git-vault test asserts one commit containing old paths, archive paths, merged file; non-git test asserts notice + clean state.

### Layer D — Cross-cutting safety gates

- [ ] **D1** `src/engram/consolidate/guards.py`: `acquire_consolidate_lock(vault)` — full-run `VaultLock` acquisition (the `sync compact` precedent); busy = `ConsolidateError` busy-vault with "run `engram daemon stop` first" (NO `--force` escape). Report mode uses C2 ro-open (no lock). -> verify: held-flock test (refusal), second-run-busy test.
- [ ] **D2** refusal gates in `guards.py`: team-write vault (members.yaml/config role), read-only vault role, cloud-sync path for `--apply` (NEW stricter-than-serve behavior, per ADR 009). -> verify: one test per refusal, asserting error codes + messages.
- [ ] **D3** daemon-interaction test: while consolidate holds `VaultLock`, a daemon spawn attempt fails cleanly (no WAL wedge, clear error). -> verify: `tests/consolidate/test_daemon_interaction.py`.

### Layer E — Diagnostics

- [ ] **E1** `src/engram/diagnostics/consolidate_checks.py`: `consolidate_journal_orphan` (journal present with incomplete entries -> WARN with resume guidance), `archive_conflict_markers` (extend conflict-marker scan to `<vault>/archive/`). Wire into `run_diagnostics`. -> verify: `tests/diagnostics/test_consolidate_checks.py`; Pattern 3 honored (no-archive-dir = OK row "skipped (no archive)").
- [ ] **E2** update `ALL_*_CHECK_CODES` tuples + count-asserting tests. -> verify: existing tuple tests green with new counts.
- [ ] **E3** machine-B scenario test: simulate post-sync state (markdown moved by consolidation commit, SQLite rows still present) -> existing `_check_orphan_sqlite_rows` fires with its standard remediation. -> verify: `tests/diagnostics/` scenario test (no new check needed — proves reuse).

### Layer F — CLI wiring

- [ ] **F1** `src/engram/cli/consolidate.py` with `register(app)`: `engram consolidate` (report) + `--apply` (gated: typed confirmation "consolidate", `--yes` bypass); flags `--vault/--config/--threshold/--contradiction-threshold/--stale-days/--max-cluster-size/--prefix/--no-llm/--report PATH`; stdout human summary; exit codes 0/1/2/3. Registered in `cli/__init__.py` IN THIS LAYER (foundation Pattern 6). -> verify: `tests/cli/test_consolidate.py` (Typer runner) + help-text test; registration asserted via app command list.
- [ ] **F2** report-mode flow: ro-open, passes, write report, human summary names report path + per-pass status + exclusions. Apply-mode flow: guards (D) -> load+verify report -> apply engine (C8/C9) -> summary of applied/skipped/failed. -> verify: CLI-level tests with tmp vault.

### Layer G — Integration tests + hermetic CLI smoke

- [ ] **G1** `tests/integration/test_consolidate_flow.py`: end-to-end report -> apply on tmp vault with synthetic embeddings (injected via storage seam; no network, no model download); asserts archive layout, index curation, report-vs-vault consistency, merged-thought provenance.
- [ ] **G2** crash-injection: interrupt apply between each pair of sub-steps (journal/capture/move/delete) via monkeypatched failure; re-run resumes; final state converges; doctor clean afterward.
- [ ] **G3** non-git vault apply; second-run-busy; stale-report (edit a thought post-report, apply skips exactly that proposal, exit 3).
- [ ] **G4** `tests/test_consolidate_cli_smoke.py` (hermetic, installed binary, foundation Pattern 5): report mode on tiny vault (pending embeddings — passes skip loudly, exit 0); exact-dup apply with `--no-llm --yes` (two identical captures -> keep-newest + archive; no model needed); daemon-running refusal (hold flock, expect exit 2); team-vault apply refusal. -> verify: all smoke scenarios assert observable state (files, exit codes, stderr classification).

### Layer H — Docs

- [ ] **H1** `docs/adr/009-consolidation.md`: decisions (archive-as-move; age-only report-only staleness; daemon-stopped one-shot concurrency; most-restrictive portability; team refusal; `.indexes/consolidate/` state; report-only contradictions; cloud-path apply refusal as new behavior; numpy promotion). Cross-link from `docs/ARCHITECTURE.md`.
- [ ] **H2** `docs/CONSOLIDATION.md`: operator guide — report walkthrough, apply flow incl. `engram daemon stop`, machine-B convergence (`engram doctor --repair --remove-orphans` verbatim), archived-PII-lives-in-git-history warning, `delete` can't-reach-archived limitation, zero-vault-mutation-but-not-zero-egress note, English-tuned embedding note.
- [ ] **H3** README (feature row + roadmap renumber note), `docs/LLM_FEATURES.md` (consolidate's LLM usage + budget), CHANGELOG `[Unreleased]` Added/Changed, CLAUDE.md "Common operations" snippet.
- [ ] **H4** PII scan + planning-vocab scan over all Phase 6 files; fix hits.

---

## Open Questions (resolved defaults — implementation proceeds with these unless redirected)

1. Staleness v1 = age-only, report-only. **Resolved: yes** (no retrieval data source exists; telemetry deferred).
2. `--apply` requires daemon stopped. **Resolved: yes for v1**; daemon-RPC routing deferred.
3. Archive dir = visible `<vault>/archive/`. **Resolved: visible** (episodic record, not machine state).
4. Contradictions report-only. **Resolved: yes.**

---

## Critique Pass (deep-plan 4th sub-agent, 2026-06-09)

| Finding | Sev | Resolution in this plan |
|---|---|---|
| Locking model mixed daemon-stopped + coordinator-pause | Blocking | Daemon-stopped one-shot adopted; commit via gitops directly (C9); no coordinator involvement |
| `--apply` semantics for stale candidates undefined | Blocking | Stale = report-only this phase (Goal + C5) |
| `capture()` can't write provenance frontmatter | Blocking | C4 seam added |
| New frontmatter fields would trip `UNKNOWN_EXTRA_FIELD` drift forever | Blocking | A3 adds them to the model + known-fields |
| "index-rows-missing-files" doctor check already exists | Should-Fix | E3 proves reuse; no duplicate check |
| `.engram/` is git-tracked; report/journal location claim false | Should-Fix | State moved to `.indexes/consolidate/` |
| "Cloud-path refusal replicated from serve" — serve only warns | Should-Fix | D2 states it as NEW behavior (ADR 009) |
| `capture()` doesn't embed; merged thoughts would land pending | Should-Fix | C8 embeds via FastEmbedProvider explicitly |
| Clustering dependency + scale bound unstated | Should-Fix | B1: numpy direct dep, hand-rolled partition, 20k guard |
| ro-open WAL edge (SQLITE_READONLY_CANTINIT) | Should-Fix | C2 explicit failure path + test |
| Non-git vaults break the apply commit | Should-Fix | C9 skip-with-notice |
| Swallowed SQLite insert could orphan merged knowledge | Should-Fix | C8 `on_index_failure='fail'` |
| No `--force` on apply; precise precedent citation; zero-mutation precision; CHANGELOG | Nice | D1 / ADR 009 / H2 / H3 |

## Sub-Agent Findings Summary

**Code analysis** (27 tool uses): mapped storage/cli/lock/daemon/llm/embedding/models/diagnostics; confirmed no retrieval tracking, no bulk-embeddings reader, advisory-only lock checks in delete/reindex, `sync compact` as the full-lock precedent. **Risk** (17 findings, 7 High): all High risks mitigated by named steps above. **Edge cases** (6 categories): 4 needs-design-decision items resolved (archive location, staleness source, portability/prefix merge semantics, machine-B convergence). **Critique** (code-reviewer): 4 Blocking + 8 Should-Fix + 5 Nice-to-Have, all incorporated (table above).

## Implementation Notes

- Dependency graph: A -> B -> C (C1-C4 before C5-C9) -> D -> E -> F -> G -> H. D depends only on A + lock.py; E depends on A4 + C8 journal shape; F depends on C/D; G depends on F.
- One commit per layer (C may split storage/engine). Commit messages follow the python-package-builder shape; signed + DCO; no planning vocabulary in source/tests/docs (plan + ADR + CHANGELOG headers exempt per repo rules).
- Layer-scoped test gates during build; full suite at F/G; coverage gate at exit.
- Checkpoint cadence: `session-wrap --checkpoint` after Layers C, F, and exit.

## Phase Exit Criteria

**Code-side** (verifiable from repo state):
1. Full suite green (baseline ~1166 + new); ruff + ruff-format + mypy strict clean.
2. Coverage >= 80% (`--cov-fail-under=80`); any `pragma: no cover` carries justification + smoke pointer (Pattern 8).
3. `tests/test_consolidate_cli_smoke.py` green against the installed binary (Pattern 5), including `python -m engram` parity intact.
4. Drift-clean: consolidated/archived files produce no doctor WARNs (A3 verified).
5. ADR 009 + CONSOLIDATION.md + CHANGELOG + README updates present; PII + planning-vocab scans clean.
6. `docs/archive/phases/PHASE_6_CODE_COMPLETE.md` authored with this split + evidence.

**Operational** (require live action):
7. Live test per engram discipline: daemon restart + `engram doctor` clean + report mode against the real personal vault; sane report.
8. Apply rehearsal on a COPY of the real vault; doctor clean after; archive layout inspected.
9. Multi-machine convergence observation (next personal-vault sync window): machine-B doctor surfaces orphan rows + repair converges.
10. PyPI release 0.6.0 (existing PUBLISHING.md flow) — separate maintainer action.

## Deferred to Phase 7+

1. Retrieval telemetry for true zero-retrieval staleness (needs reindex-surviving, per-machine-aware design).
2. Stale/contradiction apply actions (currently report-only).
3. Daemon-RPC apply routing (consolidate without stopping the daemon).
4. Restore/unarchive command (incl. `file_path` UNIQUE collision handling).
5. Team-vault apply (merged `captured_by` semantics + pre-receive hook protocol change, server-side first).
6. Cross-prefix merge flag; cross-vault consolidation (explicitly never automatic).
7. Auto-reconcile of machine-B orphan rows at serve startup.
8. Archive-aware delete (PII removal path into `archive/` + git history scrub pointer).
9. Vaults > 20k thoughts (chunked clustering).
