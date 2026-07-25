# Phase plan + code-complete archive

These are historical artifacts from the phased delivery of engram. They are preserved because:

1. The 8 ADRs in `docs/adr/` reference them as the design context.
2. The retrospectives in `<your-meta-stack-repo>/workspace/engram/PHASE_<N>_RETROSPECTIVE.md` reference them.
3. They document the load-bearing rationale behind decisions that the shipped code embodies (why a state machine over a coroutine, why `(fingerprint, source, created_at)` triple-match for migration idempotency, why two-layer enforcement at security boundaries, why per-vault UDS daemon vs per-user, etc).

**They are NOT operator documentation.** Users and contributors should look at `docs/QUICKSTART.md`, `docs/USE_CASES.md`, `docs/ARCHITECTURE.md`, the per-flow guides (`docs/MULTI_MACHINE_SETUP.md`, `docs/TEAM_BRAIN_GUIDE.md`, `docs/DAEMON_MODE.md`, etc), and the ADRs in `docs/adr/`.

## Contents

| File | What it is |
|---|---|
| `PHASE_1_PLAN.md` | Solo MVP + Open Brain migration: 8-layer build plan, risks, edge cases, verifier per step |
| `PHASE_1_CODE_COMPLETE.md` | Phase 1 exit-criteria evidence: every code-side criterion mapped to commit hash + test count |
| `PHASE_2_PLAN.md` | Multi-machine sync via git transport: 21-step plan including the SyncCoordinator state machine |
| `PHASE_2_CODE_COMPLETE.md` | Phase 2 exit-criteria evidence |
| `PHASE_3_PLAN.md` | Multi-vault + friend-share + optional LLM: 22-step plan |
| `PHASE_3_CODE_COMPLETE.md` | Phase 3 exit-criteria evidence |
| `PHASE_4_PLAN.md` | Team Brain (multi-target write + GPG attribution + per-prefix routing + server hook): 22-step plan + 7 pinned invariants |
| `PHASE_4_CODE_COMPLETE.md` | Phase 4 exit-criteria evidence |
| `PHASE_5_PLAN.md` | Daemon mode (per-vault UDS daemon + auto-spawning proxy): 8-layer plan with TDD-paired steps |
| `PHASE_5_BASELINE.md` | Pre-daemon-mode test/coverage baseline + regression-delta targets |
| `PHASE_5_FASTMCP_AUDIT.md` | FastMCP per-connection dispatch entrypoint audit; rationale for the compat shim in `src/engram/daemon/fastmcp_dispatch.py` |
| `PHASE_5_LAYER_A_AUDIT.md` | Pre-implementation audits (SQLite WAL connection model + FastMCP dispatch) captured before daemon-mode build started |

## See also

- `docs/adr/` — ADRs that cite these plans as design rationale.
- `<your-meta-stack-repo>/workspace/engram/PHASE_<N>_RETROSPECTIVE.md` — lessons learned per phase.
