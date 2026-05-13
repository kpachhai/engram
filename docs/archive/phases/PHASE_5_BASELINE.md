# Phase 5 Baseline Metrics (Pre-Implementation)

Captured: 2026-05-13 (Layer A task A0.7)

## Test counts

- Total collected: **1165 tests** (from `uv run pytest --collect-only -q`)
- Smoke tests in `tests/test_phase4_cli_smoke.py`: **15**
- Pass rate at baseline: 1164 passed, 1 failed (`tests/embedding/test_fastembed.py::test_real_fastembed_round_trip` — pre-existing failure tied to the real-network FastEmbed round-trip; not in Phase 5 scope)
- Coverage: **80%** (TOTAL 6391 statements, 1130 misses, 1728 branches, 256 partial)

## Regression deltas at Phase 5 close

Phase 5 acceptance gates:

- Total tests ≥ **1165 + 80 = 1245** (Phase 5 plan budgets ~80 new tests).
- Smoke tests ≥ **15 + 9 = 24** (Phase 5 plan adds 9 hermetic CLI smoke tests).
- Coverage remains ≥ **80%** (the existing `--cov-fail-under=80` gate).
- No new test failures introduced by Phase 5 work. The 1 pre-existing FastEmbed failure is not Phase 5's regression budget.

## Notes

The 1166-test number quoted in `idea-forge/workspace/engram/PENDING_TASKS.md` predates a small test-suite tidy after the Phase 4 close-out. Today's collection count (1165) is the authoritative pre-Phase-5 baseline used for the regression-delta math above.
