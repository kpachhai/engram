# Phase 6 - Consolidation: Code-Complete Declaration

**Date:** 2026-06-10
**Plan:** `docs/PHASE_6_PLAN.md` | **Design record:** `docs/adr/009-consolidation.md`
**Status:** Code-complete; 2 of 4 operational criteria pending (see below).

## Code-side exit criteria (verifiable from repo state)

| # | Criterion | Evidence |
|---|---|---|
| 1 | Full suite green; ruff + ruff-format + mypy strict clean | 1504 passed / 1 skipped (Linux-only SO_PEERCRED test); `ruff check` + `ruff format --check` + `mypy` all clean. Baseline at phase start was 1335 passed. |
| 2 | Coverage >= 80% | 82.12% under `--cov=src --cov-fail-under=80`. Two new `pragma: no cover` lines (`consolidate/apply.py`) guard model-validator-unreachable branches. |
| 3 | Hermetic CLI smoke vs installed binary, incl. `python -m engram` parity | `tests/test_consolidate_cli_smoke.py`: report exit 0 + loud exclusion accounting, exact-dup apply end-to-end, flock-held exit 2, team-vault exit 2, no-index remediation, typed-confirmation gate, module-form `--version` parity + subcommand resolution. |
| 4 | Drift-clean consolidated vaults | `tests/consolidate/test_frontmatter_fields.py` (no UNKNOWN_EXTRA_FIELD on archived/merged files) + `tests/integration/test_consolidate_flow.py::TestDoctorCleanAfterApply`. |
| 5 | Docs + scans | `docs/CONSOLIDATION.md`, ADR 009, CHANGELOG `[Unreleased]`, README/ARCHITECTURE/LLM_FEATURES/CLAUDE cross-refs; PII scan green at every commit (pre-commit hook); planning-vocab scan clean on new files. |
| 6 | This document | You are reading it. |

### Load-bearing invariants, each with a regression test

- **Reindex cannot resurrect archived thoughts** (incremental AND `--full`):
  `tests/integration/test_consolidate_flow.py::TestReindexDoesNotResurrect`.
- **Crash-interrupted applies converge on re-run without duplicating the
  merged thought** (journal carries the merged id through id-less entries):
  `TestCrashResume` (fail at row-delete; fail mid-archive) +
  `tests/consolidate/test_apply.py` (fail at index-insert with markdown undo).
- **Archived body bytes are untouched** (property test) and the archive
  helper is resume-idempotent: `tests/consolidate/test_storage_primitives.py`.
- **`portability=block` content never reaches an LLM** - filtered before
  pair/cluster assembly AND refused by the resolver:
  `test_block_member_cluster_never_reaches_distiller`,
  `test_block_thought_never_reaches_the_judge`.
- **Apply is daemon-safe**: full-run `VaultLock`; a daemon-spawn-shaped
  acquisition fails cleanly while apply holds the vault
  (`tests/consolidate/test_guards.py`); report mode opens read-only beside a
  live daemon (`test_opens_beside_live_writer`).
- **Stale reports cannot apply blind**: per-proposal fingerprint + snapshot
  re-verification; partial applies exit 3
  (`tests/cli/test_consolidate.py::test_stale_report_partial_apply_exits_three`).

### Spec-audit dispositions (sub-agent walk of the plan, 2026-06-10)

7 findings, none blocking; 4 closed same-day (exit-3 CLI test, cluster-cap
downgrade test, judge-side block-exclusion test, module-form parity smoke;
plus this document). Two recorded as accepted drift:

- **Report JSON carries no old-id -> merged-id map.** Merged ids do not
  exist at report time; the mapping is emitted where it becomes knowable -
  `ApplyResult.id_map` + journal entries + `superseded_by` frontmatter on
  every archived file. The plan's wording predated this realization.
- **README carries no roadmap section**, so the phase renumbering note
  lives in CHANGELOG only (same place the daemon-mode renumbering lives).

### Incidental fix shipped with this phase

Markdown rewrites (`update_metadata` / `update_body` / reindex re-capture)
previously dropped `legacy_created_at` silently because write-side extras
preservation only kept UNKNOWN fields. The serializer now owns an explicit
field set; everything else round-trips verbatim
(`src/engram/storage/markdown.py`, regression in
`tests/consolidate/test_storage_primitives.py::TestProvenanceCapture`).

## Operational exit criteria

| # | Criterion | Status |
|---|---|---|
| 7 | Live test on the real personal vault | **DONE 2026-06-10**: tool reinstalled from source; `engram daemon stop` -> `engram doctor` all green (new consolidate rows present, correctly skipped-OK); daemon restarted; report mode ran BESIDE the live daemon: 16 near-duplicate clusters (sizes 2-3), zero exclusions, sane report JSON. |
| 8 | Apply rehearsal on a vault copy | **DONE 2026-06-10**: copy seeded with an exact-duplicate pair; report -> `--apply --yes` archived the older duplicate (non-git notice correct); doctor on the copy: consolidate rows OK, only expected pending-embedding warning from the unembedded seed. |
| 9 | Multi-machine convergence observation | **PENDING** - next personal-vault sync window: confirm machine B surfaces orphan rows and `engram doctor --repair --remove-orphans` converges. |
| 10 | PyPI release 0.6.0 | **DONE 2026-07-25**: published as `engram-mcp-server` 0.6.0 (the `engram-mcp` name belongs to an unrelated project). Signed tag `v0.6.0` + GitHub release with both artifacts; clean-venv install from PyPI verified, `engram doctor` all green. |

## Explicitly NOT verified

- Real-LLM distillation/judging quality (all LLM-path tests use mocks; the
  live vault report ran `--no-llm`). First real-provider run is part of
  operational dogfood.
- Multi-machine convergence (criterion 9).
- Non-English-corpus clustering quality (documented limitation).
- Vault-scale performance beyond the real vault's size; the 20k guard is
  tested, sub-20k large-vault timing is not benchmarked.
