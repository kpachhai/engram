# ADR 006 - Multi-vault routing, friend-share via bundles, and optional LLM

**Status**: Accepted
**Date**: 2026-05-05
**Phase**: 3
**Supersedes**: none
**Superseded-by**: none

## Context

Phase 1 shipped a single-vault MCP server with markdown source-of-truth
+ sqlite-vec ANN search. Phase 2 added per-vault git-based
multi-machine sync. Phase 3 introduces three orthogonal capabilities:

1. **Multi-vault hosting** - one `engram serve` process surfaces N
   vaults under different roles (one `primary` that accepts captures,
   plus zero-to-many `read-only` mirrors). Cross-vault search
   aggregates results with vault attribution preserved.
2. **Friend-share via bundles** - point-in-time `engram export` /
   `engram import` flows that move thoughts between vaults via a
   tar.gz manifest. NOT live git-pull from a friend's remote.
3. **Optional LLM-mediated tools** - `summarize_thought` and
   `synthesize_thoughts` MCP tools backed by a configurable provider
   (Anthropic / OpenAI / Ollama / llama.cpp / OpenAI-compatible) with
   strict per-thought portability gates and a daily cost cap.

The design must preserve Phase 1+2 client semantics (search defaults
to the primary vault; existing wire shapes unchanged) while opening
the new surfaces additively (`tools/list` advertises 7 tools instead
of 5 - `listChanged`-compatible additive change).

## Decisions

### D1 - Aggregator: ATTACH for <=10 vaults, SEQUENTIAL beyond

`engram.multivault.aggregator.aggregate_search` reports `mode_used =
"ATTACH"` when fewer than 11 vaults are mounted and
`force_sequential` is False; `"SEQUENTIAL"` otherwise. Operators see
the active mode in `engram doctor`'s `aggregator_mode` INFO row so a
latency cliff at the 11th vault is observable rather than mysterious.
Phase 3 implementation runs per-storage queries sequentially under
both labels (the literal SQLite `ATTACH DATABASE` optimization is
deferred to a Phase 4 perf pass when a real workload demands it); the
labels accurately surface eligibility, not implementation.

### D2 - Per-thought (not per-vault) portability gate at the LLM layer

The LLM resolver (`engram.llm.resolver.resolve_provider`) inspects
each candidate thought's `portability` value:

* `block` thoughts ALWAYS refuse (`BlockThoughtLLMDisallowed`); no
  flag, no provider locality, no per-vault override grants
  exemption. This is the absolute floor.
* `sensitive` thoughts require a local provider (`is_local=True`);
  remote providers refuse with
  `sensitive_thought_remote_provider_disallowed`.
* `portable` thoughts permit any configured provider.

The check is per-thought because a single search result set can mix
portability tiers across vaults; per-vault gating would either
over-block (one sensitive thought disqualifies the whole vault) or
under-block (a sensitive thought hides behind a portable-vault
label).

### D3 - Bundle-based friend-share, not git-pull subscription

Friend-share runs through `engram export` + transport channel +
`engram import`. The Phase 3 plan documents the rationale: a friend's
git history is attacker-influenceable (account compromise, sloppy
hygiene) and contains arbitrary markdown that `engram` would index
with neither path-traversal validation, per-file size caps, YAML
safe-load enforcement, nor `portability=block` filtering at ingest.
The bundle import gate is the only place
`docs/superpowers/specs/2026-05-04-engram/06-SECURITY.md` lines 31-44
(per-file 1 MB, per-bundle 4 GB streaming, `safe_load`,
path-traversal refusal, id-collision refusal, block filter) can be
applied to friend-derived content. Live-pull is deferred to Phase 4
where capability-token-based vault sharing can layer on.

### D4 - Provider abstraction + separate trust file for `base_url`s

`engram.llm.protocol.LLMProvider` is the narrow async Protocol
adapters implement. Five concrete adapters ship: Anthropic, OpenAI,
Ollama, llama.cpp, OpenAI-compatible (custom `base_url`). The
custom-`base_url` case validates against
`~/.config/engram/trusted-llm-urls.yaml` BEFORE construction;
unknown URLs refuse with `base_url_not_trusted`. Three default
patterns are baked in (`localhost`, `api.anthropic.com`,
`api.openai.com`). Adding a custom pattern is a separately-
acknowledged trust gate documented in `LLM_FEATURES.md`.

### D5 - Read-only vaults: read-path only

Mounting a vault under `role: read-only` sets the
`read_only_role: bool` flag on the storage facade. Every public
write entry-point (`capture`, `update_metadata`, `update_body`,
`delete`, `repair_pending_embeddings`, `reindex_vault`) refuses with
`VaultReadOnlyError` (hard refusal, not soft skip). Doctor catches
the exception when the maintainer runs `--repair` and surfaces a
"skipped N pending embeddings on read-only vault X" INFO row.

### D6 - 5 stable + 2 additive MCP tools; friend-vault default-off

The five Phase 1 tools (`capture_thought`, `search_thoughts`,
`list_thoughts`, `thought_stats`, `fetch`) keep their wire shapes.
Phase 3 adds `summarize_thought` (per-thought summary) and
`synthesize_thoughts` (cross-vault RAG). The synthesizer's
`include_friend_vaults` defaults to `False` per the B-4 fix: a
crafted prompt-injection in a friend's body can NOT reach the LLM
context unless the operator explicitly opts in. When opted in,
prompt assembly wraps every retrieved thought in
`<thought id="..." vault="..." source="...">` delimiters and the
system prompt instructs the model to ignore in-content directives.
The citation post-validator strips any UUID the LLM cites that
wasn't in the actually-retrieved set (R-M8 / Q5 strip-default).

### D7 - Cycle detection by `bundle_id` chain, not by `source_user`

Imported thoughts inherit a chain `source: bundle:<id> <- bundle:<id>
<- ...`. The importer walks every existing thought's chain looking
for the candidate `manifest.bundle_id`; if found, refuses with
`BundleCycleDetected`. This admits multi-machine same-user imports
(every export gets a fresh UUID-v7 `bundle_id`) while still catching
A->B->C->A loops at the third hop.

### D8 - LLMBudget cost-cap state lives under the primary vault

`<primary>/.indexes/llm_usage.json` persists the per-day cost
ledger so cap state survives serve restarts. When the primary vault
changes (operator edits per-user config), prior cost data lives at
the OLD primary's index dir; engram does NOT auto-migrate (low
impact since the cap resets daily; documented for operator
awareness). Atomic writes via `atomic_write_text` ensure a crash mid
write leaves either the previous-good or new-good state.

## Consequences

* **Performance**: Phase 3 cross-vault search is sequential at the
  per-storage level; a real ATTACH-based optimization is deferred.
  At <=10 vaults this is undetectable; at the 11th vault and beyond
  the latency cliff is observable in `engram doctor`'s
  `aggregator_mode` row.
* **Backwards compatibility**: Phase 1+2 clients see 5 tools and the
  same wire shapes. Phase 3-aware clients see 7 tools and may pass
  `filter.vault = "*"` for cross-vault search.
* **Threat model boundary**: friend-vault content is treated as
  attacker-influenceable. Default-off RAG inclusion + delimiter wrap
  + citation strip is the ratchet, not a guarantee. ADR explicitly
  documents prompt injection as unsolved at the model layer.
* **Operational state**: cost cap, bundle migration reports, and
  per-vault locks all live under each vault's `.indexes/` so
  multi-machine sync (Phase 2 baseline) sees them but git-ignores
  them.

## Alternatives considered

* **Live git-pull friend-share** (rejected per D3 / Q1).
* **Per-vault portability gate at LLM layer** (rejected per D2).
* **Inline `allowed_base_urls` config field** (rejected per SF-9 in
  the Phase 3 plan; replaced with separate trust file requiring a
  confirmation step).
* **Capture LLM output back into the vault as `[Synthesis]` thoughts**
  (rejected per Q7; erases the difference between "I thought this"
  and "the model thought this"; operator can manually capture if
  desired).

## See also

* `docs/PHASE_3_PLAN.md` - the implementation plan this ADR locks in.
* `docs/PHASE_3_CODE_COMPLETE.md` - exit-criteria evidence.
* `docs/MULTI_VAULT_SETUP.md` - operator setup guide.
* `docs/FRIEND_SHARE_GUIDE.md` - export / import flow walkthrough.
* `docs/LLM_FEATURES.md` - provider config + portability rules.
* `docs/adr/005-sync-coordinator.md` - Phase 2 baseline this phase
  scales.
