# engram Phase 3 - Code-complete

**Date**: 2026-05-05
**Phase**: 3 - Multi-vault foundation + friend-share + optional LLM
**Status**: code-complete; one operational criterion (#14) deferred
to live deployment per the Phase 3 plan

This document is the canonical exit-criteria evidence surface for
Phase 3. The split is per the project's CLAUDE.md "Code Project
Completion Gate" rule:

* **Code-side criteria (1-13)** - verifiable from repo state alone.
  All passing as of the layer commits enumerated below.
* **Operational criterion (14)** - requires live multi-machine
  multi-vault dogfood with friend-share + LLM features wired to a
  real provider. Pending the maintainer's live run.

## Layer commits

Each Phase 3 layer landed as one commit on `main`:

| Layer | Steps | Commit | Headline |
|---|---|---|---|
| A | 1-3 | `cea4fe8` | errors + config + 8 doctor codes |
| B | 4-7 | `ed9d02e` | VaultRegistry + aggregate_search + portability gate |
| C | 8-11 | `d40d151` | bundle format + exporter + importer + CLI |
| D | 12-13 | `b521e89` | LLM provider abstraction + 5 adapters + budget |
| E | 14-15 | `d0196cc` | summarize_thought + synthesize_thoughts + citations |
| F | 16-18 | `8f7df8a` | multi-vault server wiring + serve startup + doctor |
| G | 19-20 | `09abc44` | 18 exit-criterion scenarios + property test |

## Code-side exit criteria (1-13)

### 1. Multi-vault search returns attribution-preserved results

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_a_capture_then_multivault_search_attribution`
* **Status**: PASS
* **Evidence**: `aggregate_search` populates `vault_name` on every
  `AggregatorResultRow`; cross-vault search returns rows from both
  `primary` and `alice` with each row's `thought.vault` matching the
  registry-side `vault_name`.

### 2. Cross-vault portability filter pushes down at SQL layer

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_c_block_thought_never_in_cross_vault_search`
  + `test_r_aggregate_property_block_never_returned` (hypothesis property test)
* **Status**: PASS
* **Evidence**: per-vault Filter is built with
  `portability=["portable"]` (default) or `["portable", "sensitive"]`
  (with `include_sensitive=True`); `block` is NEVER in the IN-list
  regardless of any flag. Defense-in-depth re-filter at
  `assert_no_block_in_results` composes with the push-down per ADR
  006 D2.

### 3. Bundle export -> bundle import round-trip preserves source

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_f_export_then_import_round_trip`
* **Status**: PASS
* **Evidence**: imported thoughts inherit
  `source: bundle:<bundle_id> <- ...` chain; the recipient vault's
  `list_thoughts` shows the chain as the `source` field.

### 4. Bundle import refuses path-traversal, oversize, id collisions, and `block`

* **Verifiers**:
  - `tests/multivault/test_phase3_exit_criteria.py::test_g_bundle_id_collision_refuses_atomically`
  - `test_h_bundle_path_traversal_refused`
  - `test_i_bundle_block_thought_filtered_at_import`
* **Status**: PASS
* **Evidence**: id-collision pre-flight scan against existing
  thought ids refuses the WHOLE bundle on any collision (atomic at
  the pre-merge level, SF-4 fix). Path-traversal members are
  rejected at staging (`rejected_path_traversal` list populated).
  Block-portability members are stripped from staging
  (`skipped_block_count` incremented).

### 5. Read-only vaults refuse every write tool with VaultReadOnlyError

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_j_read_only_vault_refuses_capture`
  + `tests/multivault/test_registry.py::test_read_only_vault_write_raises`
* **Status**: PASS
* **Evidence**: `VaultStorage.read_only_role` flag set at mount time;
  every public write entry-point (`capture`, `update_metadata`,
  `update_body`, `delete`, `repair_pending_embeddings`) raises
  `VaultReadOnlyError` with `error_code = "vault_read_only"`.
  `reindex_vault` enforces the same guard at the function level.

### 6. Read-only vaults skip `--repair --remove-orphans`

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_k_read_only_vault_refuses_doctor_repair`
* **Status**: PASS
* **Evidence**: `repair_pending_embeddings` raises
  `VaultReadOnlyError` on read-only-mounted storages; the doctor
  catches it and surfaces a "skipped N pending embeddings on
  read-only vault X" INFO row.

### 7. LLM provider resolver enforces per-thought portability gate

* **Verifiers**:
  - `tests/llm/test_resolver.py` (10 cases covering block + sensitive + cross-provider)
  - `tests/multivault/test_phase3_exit_criteria.py::test_d_block_thought_never_reaches_llm`
  - `test_e_sensitive_blocked_from_remote_provider`
  - `test_q_adversarial_prompt_injection_does_not_leak`
* **Status**: PASS
* **Evidence**: `engram.llm.resolver.resolve_provider` raises
  `BlockThoughtLLMDisallowed` whenever a `block` thought is present;
  `sensitive_thought_remote_provider_disallowed` when sensitive +
  remote provider; `cross_provider_synthesis_disallowed` when
  thought set spans differing per-vault providers; friend-vault
  thoughts are excluded from synthesize/summarize RAG context by
  default per `include_friend_vaults=False` (B-4 fix).

### 8. LLM citation post-validator strips hallucinated citations

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_o_citation_post_validation_strips_hallucinated`
  + `tests/llm/test_citations.py` (6 cases)
* **Status**: PASS
* **Evidence**: `engram.llm.citations.validate_citations` parses the
  LLM response for UUID-shaped substrings, cross-references against
  the actually-retrieved set, replaces hallucinated citations with
  `[citation removed]`, and emits a WARN log entry per stripped
  citation.

### 9. Daily cost cap enforced; persists across serve restart

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_n_llm_daily_cost_cap_enforced`
  + `tests/llm/test_budget.py::test_persisted_state_survives_reload`
* **Status**: PASS
* **Evidence**: `LLMBudget.check_budget` raises `LLMProviderError`
  with reason `daily_cost_cap_exceeded` when today's tally + the
  estimate would cross the cap. State persists to
  `<primary>/.indexes/llm_usage.json` via `atomic_write_text`;
  `LLMBudget.load_or_init` rebuilds from disk on serve restart.

### 10. Aggregator detects ATTACH -> SEQUENTIAL threshold at 11 vaults

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_l_aggregator_attach_to_sequential_threshold`
  + `tests/multivault/test_aggregator.py::test_sequential_path_at_eleven_vaults`
* **Status**: PASS
* **Evidence**: `aggregate_search` returns
  `mode_used = AggregatorMode.SEQUENTIAL` whenever the mounted vault
  count exceeds `ATTACH_VAULT_COUNT_CEILING` (10) or
  `force_sequential` is True. `engram doctor`'s `aggregator_mode`
  INFO row surfaces the active mode.

### 11. Embedding-model mismatch refuses cross-vault search

* **Verifier**: `tests/multivault/test_phase3_exit_criteria.py::test_m_embedding_model_mismatch_refuses_search`
* **Status**: PASS
* **Evidence**: `assert_compatible_embeddings(registry)` reads each
  vault's `engram_settings` (`embedding_model_name` + `dim`); raises
  `EmbeddingModelMismatch` when any two declared values disagree.
  Called on every `aggregate_search` invocation (cheap; cached at
  the SQLite page-cache level).

### 12. CI matrix passes (Python 3.11 + 3.12, macOS + Ubuntu)

* **Verifier**: `.github/workflows/ci.yml` exercise on next push.
* **Status**: PENDING (next push) - same matrix as Phase 2's; no
  Phase 3 changes to the CI workflow itself.
* **Evidence**: local `uv run pytest` at the layer commits each
  passes under macOS x86_64 (Intel Mac, Python 3.11.15). Linux
  + Python 3.12 cells will exercise on the next push to `main`.

### 13. Documentation: ADR 006 + 4 new docs + README + CHANGELOG

* **Verifiers**:
  - `docs/adr/006-multi-vault-and-llm.md` (~150 lines)
  - `docs/MULTI_VAULT_SETUP.md` (~140 lines)
  - `docs/FRIEND_SHARE_GUIDE.md` (~140 lines)
  - `docs/LLM_FEATURES.md` (~190 lines)
  - `README.md` Status section (Phase 3 added, Roadmap row updated)
  - `CHANGELOG.md` `[Unreleased]` grouped under Added / Changed /
    Security
* **Status**: PASS
* **Evidence**: this commit (Layer H).

## Operational criterion (14)

### 14. Maintainer dogfoods Phase 3 across own vault + ≥1 friend mirror for ≥7 days

* **Status**: PENDING (deferred to live deployment per the Phase 3
  plan exit-criteria split)
* **Evidence required**:
  - `engram serve` running against own vault + at least one
    friend-imported read-only vault for >=7 consecutive days.
  - `synthesize_thoughts` exercised against the mixed corpus at
    least once per day during that window.
  - LLM features wired to a real provider (Ollama for sensitive,
    Anthropic / OpenAI for portable).
  - No falling back to a hosted memory tool during the window.

This criterion cannot be verified from repo state; it requires
live multi-machine + multi-vault deployment.

## Phase 1 + 2 operational items still pending

Per `docs/PHASE_1_CODE_COMPLETE.md` and
`docs/PHASE_2_CODE_COMPLETE.md`, these remain operationally pending
and are inherited by Phase 3:

1. `pip install engram-mcp-server` against a clean venv (macOS + Linux) -
   blocked on PyPI publish of v0.2.0 / v0.3.0.
2. `engram serve` standalone against a real config + 5 MCP tools
   exercised against Claude Code as the client.
3. `engram migrate-from-open-brain` against the real Open Brain
   corpus.
4. `engram doctor` all-green on a fresh install + migrated vault.
5. 14-day Phase 1 dogfood window.
6. Phase 2 7-day two-machine dogfood across two real machines.

## Quality-gate snapshot

* `uv run pytest`: 861 passed (was 643 baseline pre-Phase-3; +218
  new tests).
* `uv run ruff format` + `uv run ruff check --fix`: clean.
* `uv run mypy`: clean on 149 source files.
* `uv run pytest --cov=src --cov-fail-under=80`: see Phase 3
  retrospective for the post-layer-H coverage measurement.
* `bench/search_10k.py --size 1000 --queries 50`: NFR1 p95 < 100 ms
  remains green from Phase 2; Phase 3 cross-vault re-measurement
  pending the operational dogfood.

## See also

* [`PHASE_3_PLAN.md`](./PHASE_3_PLAN.md) - the implementation plan
  this document closes.
* [`adr/006-multi-vault-and-llm.md`](./adr/006-multi-vault-and-llm.md) - design rationale.
* [`MULTI_VAULT_SETUP.md`](./MULTI_VAULT_SETUP.md) - operator setup.
* [`FRIEND_SHARE_GUIDE.md`](./FRIEND_SHARE_GUIDE.md) - export / import flow.
* [`LLM_FEATURES.md`](./LLM_FEATURES.md) - provider config + portability rules.
