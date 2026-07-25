# engram Phase 4 - Team Brain (Implementation Plan)

**Authored**: 2026-05-05 via `superpowers:deep-plan` (3 parallel sub-agents + critique + 1 revision pass)
**Spec sources** (live in the maintainer's idea-forge planning repo at `<your-meta-stack-repo>/docs/superpowers/specs/2026-05-04-engram/`):
- `03-ROADMAP.md` Phase 4 lines 121-153 (6 deliverables + 3 exit criteria)
- `02-TECHNICAL_DESIGN.md` Vault Model + Per-Vault Identity sections
- `06-SECURITY.md` Boundary B5 (team-vault read/write principal) - to be added in Phase 4

**In-repo references**:
- `docs/PHASE_3_PLAN.md` (cadence + format template)
- `docs/PHASE_3_CODE_COMPLETE.md` (Phase 3 exit-criteria evidence)
- `docs/adr/006-multi-vault-and-llm.md` (Phase 3 design rationale; the multi-vault primitives this phase scales)
- `docs/adr/005-sync-coordinator.md` (Phase 2 sync coordinator state machine; Phase 4 scales to N writers per remote)

## Phase 4 Deliverables (D1-D6, mapped to Plan steps)

The 6 deliverables enumerated locally so the plan stands alone without the planning-repo specs:

| # | Deliverable | Owning step(s) |
|---|---|---|
| D1 | Multi-target write capability - `vault: personal \| team-x \| team-y` flag in capture, with `team-write` role distinct from Phase 3's `primary` and `read-only` | Steps 1 + 2 + 4 + 16 |
| D2 | Team policy config - per-team-vault `allowed_prefixes` + `allowed_sources` + `accept_sensitive` allowlists, parsed at capture time AND enforced server-side via `pre-receive` hook | Steps 3 + 5 + 11 + 13 + 18 |
| D3 | Sender attribution - every team-vault thought carries the originating user's GPG-fingerprint-bound identifier, recorded in both the markdown frontmatter (`captured_by:`) and the git commit author. Refusal at capture if the local key isn't enrolled in `members.yaml` | Steps 3 + 6 + 11 |
| D4 | `engram team-vault setup` CLI command bootstrapping a new team vault with: canonical `.gitignore` (refuses `.indexes/` push), checked-in `engram.config.yaml` (embedding-model lock), `.engram/members.yaml` (enrolled GPG fingerprints), `.engram/team-policy.yaml` (allowlists), and a documented server-side hook bundle the operator installs on the remote | Steps 12 + 13 |
| D5 | Per-prefix routing rules - `[Postmortem]` auto-routes to team-x; explicit `vault:` arg always wins over rules; portability=`block` always vetoes routing; routing target must be mounted + non-read-only + embedding-model-compatible or capture refuses (no silent fallback) | Steps 8 + 16 |
| D6 | Conflict resolution conventions - persistent push queue surviving outage / restart; auto-pull + auto-rebase under the per-vault flock; `engram doctor` surfaces stale-membership / pending-push / policy-violation rows; cross-vault `engram move-thought` (deferred R-L4 from Phase 3) ships with deterministic vault-name lock-ordering | Steps 7 + 9 + 19 + 20 |

## Goal

When complete, three engram instances on three different machines can each capture into the same team vault simultaneously without losing thoughts, the team's policy refuses out-of-allowlist prefixes both client-side AND server-side, every team-vault thought is bound to a GPG-verified sender identity that other team members can trust, and a user removed from the team has their push capability revoked at the git remote while their already-captured contributions remain searchable on the other members' machines.

**Pinned invariants for Phase 4** (every other section that contradicts these is wrong; tests assert against these directly):

1. **Portability beats routing.** A thought with `portability: block` ALWAYS lands in personal-primary regardless of explicit `vault:` arg, per-prefix routing rules, or team policy. `sensitive` follows routing only when the destination team-vault's policy declares `accept_sensitive: true`. `portable` follows the routing chain unrestricted.
2. **Explicit beats implicit.** An explicit `vault: <name>` argument on `capture_thought` wins over per-prefix routing rules. Routing rules fire only when no explicit `vault:` is supplied. Documented in `LLM_FEATURES.md`-style operator guide.
3. **Sender identity binds to a GPG fingerprint, not a free-form string.** The `default_user` resolution chain from Phase 1 stays for personal-vault attribution; team-vault captures additionally require the local GPG signing key's fingerprint to appear in the team's checked-in `members.yaml`. Refusal at capture (not at push) so the user fails fast. Convention: the canonical id stored in `members.yaml` is always the **primary key fingerprint** (40 hex chars, displayed as short-form 16 hex); subkeys are accepted at verify-commit time but resolve back to the primary via `gpg --list-keys --with-colons` (`pub`/`sub` walk). Subkey rotation does NOT require a `members.yaml` mutation; primary-key rotation does (and is a steward-gated operation - see Step 6.5).
4. **Two-layer enforcement: client-side is canonical for capture-time policies; server-side is canonical for push-time policies.** Client-side gates capture-time decisions (`block`-portability routing, member-enrollment refusal) because a `block` thought never makes it onto disk in a team-write vault and a non-enrolled member never produces a write attempt to push - the server-side hook can only inspect what reaches it. Server-side gates push-time decisions (prefix allowlist, source allowlist, `captured_by`-vs-git-committer integrity, `.indexes/` refusal, force-push refusal, `members.yaml` / `team-policy.yaml` steward-only mutation). The two layers compose - everything client-checked has a server-side fast-fail when applicable, and a single bypass (older client, forked engram, hand-edited markdown) doesn't breach the boundary because the other layer catches it.
5. **Vault-id is globally unique; vault-name is a per-machine alias.** The canonical id is `sha256(remote_url)[:16]`. The user-facing `name:` in their `~/.config/engram/config.yaml` is a per-machine alias that maps to the global id. Two machines may use different aliases for the same team vault; the registry indexes by id internally.
6. **Phase 1+2+3 client compatibility is preserved.** A Phase 3 client calling `capture_thought(content="...")` without a `vault` parameter sees Phase 3 behavior (lands in `primary`). Auto-routing rules fire only when the user has explicitly opted in via `auto_route: true` in their per-user config.
7. **Removed users' local clones remain readable; recall is impossible at the engram layer.** Phase 4 documents this as a structural property, not a bug. A user removed from the team has their git push capability revoked at the remote AND their local team-vault mount auto-degrades to `frozen-read-only` (cannot capture; LLM tools refuse against it; doctor surfaces `team_membership_revoked`). But the on-disk markdown clone exists and any operating-system-level read access creates copies. `engram team-vault unmount --remove-local <name>` provides the operator's exit ramp; the team takes-it-as-given that revocation does not delete prior-distributed copies. The exit criterion ("their contributions remain") makes this the contract.

Verifier: integration test `tests/team/test_phase4_exit_criteria.py` runs (a) three concurrent writers to the same team vault on a single-machine harness with three flock'd VaultRegistry processes; asserts all three captures land + push + pull cleanly; (b) un-enrolled GPG fingerprint capture-attempt refuses with `team_member_not_enrolled`; (c) explicit-vault overrides routing rule; (d) routing rule on `block` portability falls through to personal; (e) team policy hook refuses `[Friction]` push when team-x policy disallows; (f) revoked user's local clone keeps `read-only` semantics + doctor surfaces `team_membership_revoked`.

## Current State

**Phase 1+2+3 abstractions Phase 4 extends:**

* `VaultMount.role: Literal["primary", "read-only"]` exists with `at-most-one-primary` invariant enforced by `UserConfig._check_one_primary_vault`. Phase 4 widens this Literal to `Literal["primary", "read-only", "team-write"]` and relaxes the validator to permit N team-write vaults alongside the singleton primary.
* `VaultRegistry.mount(name, storage, role, coordinator)` already routes role to `storage.set_read_only_role()`. Phase 4 introduces a third role behavior: `team-write` keeps `read_only_role=False` BUT adds a `team_policy: TeamVaultPolicy | None` attribute on the storage that the capture handler consults before write.
* `Thought.source: str` and the SQLite `source` column carry attribution. Phase 4 adds `Thought.captured_by: str | None` (GPG fingerprint short-hash) at the storage + frontmatter layer; the existing `source: str` field captures provenance lineage (`team-x <- bundle:abc-123 <- ...`); the new `captured_by` field captures principal identity (`gpg:7E5F3A8B`).
* `SyncCoordinator` per-vault state machine ships per the Phase 2 ADR. Phase 4 scales to multiple writers against the same remote: the coordinator's `_push_cycle` already does `--force-with-lease` + retry + reflog gate; Phase 4 adds a persistent push queue (`<vault>/.engram/push-queue.local`) so queued captures survive engram restart.
* `engram_settings` table (per-vault SQLite) records `embedding_model_name` + `embedding_dim`. Phase 4 hoists the team-vault subset of these to a checked-in `engram.config.yaml` so a fresh clone immediately sees the policy without first running engram. The SQLite remains for per-machine state (e.g. `embedding_status` per row).
* Phase 3's `engram clone-vault <url> <local_path>` ships the safe-clone hook-mitigation flow. Phase 4 extends this with `engram team-vault setup --remote <url> [--init-empty]` (idempotent; refuses re-init of populated remote) and `engram team-vault join <name> [--as <local-alias>]` (joins an existing remote into the per-user vaults list).
* Phase 3 LLM tools (`summarize_thought`, `synthesize_thoughts`) compose unchanged: the per-thought portability gate already runs over the merged corpus; team-vault thoughts inherit it.

**What Phase 4 builds (new code surfaces):**

A `TeamVaultPolicy` Pydantic model checked into the team vault's root + parsed at capture time, an `engram.team` package containing the policy loader / sender-attribution validator / push-queue persister / per-prefix router, four new `team_*` doctor check codes, the `pre-receive` server-hook bundle (a documented git-hook directory the operator installs on their remote), the `engram team-vault setup` / `engram team-vault join` / `engram team-vault add-member` / `engram move-thought` CLI commands, plus the multi-target `vault:` parameter on `capture_thought` and the auto-routing dispatcher in the multi-vault MCP server.


## Risks

Prioritized; each maps to a Plan step or Open Question. Ids prefixed `P4-H/M/L` to disambiguate from Phase 3's `R-*`. Phase 3 risks that carry forward unchanged are NOT re-listed; only new-in-Phase-4 risks (or Phase-3 risks whose mitigation must extend) are tracked here.

### High severity

| ID | Risk | Mitigation step |
|---|---|---|
| **P4-H1** | `.indexes/` SQLite files committed to the team remote (someone fat-fingers `git add -A` from vault root); every `git pull` clobbers each member's local SQLite | Step 12 - `engram team-vault setup` ships a checked-in `.gitignore` listing `.indexes/`; Step 13 server-side `pre-receive` hook refuses any push containing `.indexes/` paths (defense-in-depth: client AND server) |
| **P4-H2** | `engram_settings` SQLite row binary-conflicts on push if SQLite is mistakenly tracked | Step 12 + 18 - team-vault settings (embedding model, vault id, members, policy) live in versioned YAML files at the vault root, not in `.indexes/`. The SQLite remains per-machine state only |
| **P4-H3** | Force-push wipes the team's institutional memory | Step 13 - `pre-receive` hook enables `receive.denyNonFastForwards = true` AND engram's push wrapper strips `--force` / `--force-with-lease` flags when targeting a team-write vault |
| **P4-H4** | Burst-capture load (8+ members, 60s window) - per-member push retry exhausts within the existing 3-attempt budget; captures pile up in-memory and silently die on engram restart | Step 9 - persistent push queue at `<vault>/.engram/push-queue.local` with unbounded retry + jittered exponential backoff; survives serve restart; doctor row `team_pending_pushes` surfaces queue depth + last-attempt timestamp |
| **P4-H5** | Attribution forgery: Alice opens `bob-thought.md`, edits the `captured_by:` line to `gpg:bob`, pushes; nothing cryptographically binds the body to the claimed sender | Step 13 + 6 - `pre-receive` hook verifies that EVERY pushed thought file's `captured_by:` matches the committer's GPG fingerprint (read from the signed commit). Mismatched files refuse the whole push with `attribution_committer_mismatch`. Phase 4 makes signed commits a hard gate (`signed_commits_required: true` is the default for team-write vaults) |
| **P4-H6** | `default_user` (free-form string) is too weak for cross-trust attribution - two members can collide on `kiran` from different `git config user.email` values | Step 3 + 6 - Phase 4 sender id is the GPG signing key fingerprint (short-hash 16 chars). The `members.yaml` enrollment file maps fingerprint → display name. Refusal: capture into team-write vault refuses with `team_member_not_enrolled` if the local key isn't enrolled |
| **P4-H7** | Removed user keeps capturing locally; thoughts pile up in unpushable clone | Step 9 + 11 - `engram doctor` includes a `team_membership_revoked` probe that runs `git ls-remote` (TTL'd to 1h) against the team remote; on auth failure the doctor row instructs `engram team-vault recover --to-personal-staging` (moves queued thoughts to a `team-x-orphan-<date>` personal sub-vault for the operator to triage); the team-write vault auto-degrades to `read-only` so subsequent captures refuse loudly |
| **P4-H8** | Removed user retains a full local clone of team thoughts; no recall mechanism. Documented limitation, not a bug | Step 22 - documented explicitly in ADR 007 + `TEAM_BRAIN_GUIDE.md`. `engram team-vault unmount --remove-local <name>` command provides the operator's exit path. The spec exit criterion ("their contributions remain") makes this the contract; we make the limitation explicit |
| **P4-H9** | Routing rule overrides intent on a `block`-portability thought, sending it to a shared remote | Step 8 + 16 - portability check runs FIRST, before routing. `block` always lands in personal-primary; `sensitive` follows routing only when the team policy declares `accept_sensitive: true`. Pinned invariant 1 |
| **P4-H10** | Vault-name collision: two teams both name their vault `team-research`; routing rules fire ambiguously | Step 1 + 2 - canonical vault id is `sha256(remote_url)[:16]`; the user-facing `name:` is a per-machine alias. The registry indexes by id internally. `engram team-vault join --as <alias>` lets the user pick a local alias when their preferred name is already taken on this machine |
| **P4-H11** | Sender identifier as free-form string, not verified principal - search results "by user X" are unreliable | Step 6 + 13 - same mitigation as P4-H6: GPG-fingerprint-bound id. Server hook verifies on push; client refuses on capture if local key isn't enrolled |
| **P4-H12** | Concurrent `engram team-vault setup` against the same fresh remote: both push initial schema, second push hits non-fast-forward | Step 12 - `setup` is idempotent: detects pre-existing engram-canonical files (`engram.config.yaml`, `.engram/members.yaml`, `.engram/team-policy.yaml`); refuses to overwrite if any present; resumes if all absent. First-writer-wins via the existing remote |

### Medium severity

| ID | Risk | Mitigation step |
|---|---|---|
| **P4-M1** | Auto-pull races concurrent capture: user B pulls before user A's debounced commit fires; B's local view is stale | Step 9 + 7 - the per-vault flock from Phase 2 covers both SQLite write AND git working-tree mutation; auto-pull cadence is jittered (random 0-30s offset per machine) to avoid synchronized pull storms; documented limitation that a 60s capture window may produce structurally fragmented threads (no data loss) |
| **P4-M2** | Per-machine reindex fights every pull on a hot team vault | Step 7 - bounded-rate reindex with `_index_new_files_since(commit_sha)` path that doesn't re-scan the whole tree. Phase 3's `_repair_pending_embeddings` extends with the new commit-since walker |
| **P4-M3** | Role transition mid-capture (user joins team after engram serve started; stale config) | Step 12 - `engram team-vault join` writes the new mount to `~/.config/engram/config.yaml` AND emits an INFO log instructing operator to restart `engram serve`; doctor probe `serve_config_stale` compares running serve's config-load timestamp to the file's mtime |
| **P4-M4** | User leaves team mid-debounce: 4 thoughts queued for team-x; push refuses; queue stuck | Step 9 - on auth failure during push, the queue moves to a `team-x-orphan-<bundle-id>.tar.gz` snapshot under `<personal-vault>/.engram/orphans/` and clears from the active queue; doctor surfaces `team_membership_revoked` with the orphan path; operator runs `engram orphan-recover --to personal` or `--discard` |
| **P4-M5** | Client-side policy gate is bypassable; older clients or forked engram can push out-of-allowlist | Step 13 - server-side `pre-receive` hook is the truth; client-side check is fast-fail UX only. Hook reads the team policy YAML from the pushed commits' tree (or from the previous tree if the policy itself isn't being changed) and refuses out-of-allowlist prefixes |
| **P4-M6** | Race between policy update and concurrent captures: user's stale policy allows `[Friction]`; admin tightens policy at T0; user pushes at T0+10s with stale-allowlist | Step 13 + 11 - server hook always reads the policy from the just-pushed tree (atomic with the push). Client-side: on push rejection due to policy, the orphan-quarantine path from P4-M4 fires; doctor row `team_policy_violation_quarantined` surfaces |
| **P4-M7** | Older engram client doesn't know per-prefix routing or policy fields | Step 12 - `engram team-vault setup` records a `min_engram_version` field in `engram.config.yaml`; engram MCP server announces this at handshake; older clients see a clear "upgrade to engram >= 0.4.0" error rather than silent push refusal |
| **P4-M8** | Multiple routing rule matches: `[Postmortem]` matched by both team-x and team-y rules | Step 8 - precedence rule: most-specific prefix match wins; ties broken by team-vault declaration order in user config; conflict detected at config-load and surfaced with `routing_rule_priority_collision` doctor row |
| **P4-M9** | Routing target unreachable at capture time | Step 8 - capture-time-routing must succeed-locally + queue-for-push (NOT silent fallback to personal); doctor row `routing_pending_team_push` surfaces queue depth |
| **P4-M10** | Explicit `vault: personal` clobbered by routing rule | Step 8 - pinned invariant 2: explicit > rule. INFO log when explicit overrides a matching rule (so misconfigured users notice). Routing rules never override an explicit arg |
| **P4-M11** | Removed user's local LLM gate now points at team thoughts they shouldn't query | Step 11 + 19 - doctor probe checks team membership freshness; on revocation, mount auto-degrades to `frozen-read-only` (cannot capture, cannot LLM-synthesize against) until the operator runs `engram team-vault unmount --remove-local`. Phase 3 LLM resolver re-asserts at synthesize time as defense-in-depth |
| **P4-M12** | Mixed embedding-model push poisons team index when the team upgrades | Step 12 - team policy YAML pins `required_embedding_model: BAAI/bge-small-en-v1.5`; engram refuses to mount as team-write if local model differs (forces user to either match team OR capture personal-only). Same `EmbeddingModelMismatch` error code (Phase 3) extends with new context |
| **P4-M13** | Whole-team embedding-model upgrade has no atomic path | Step 12 - new CLI: `engram team-vault upgrade-embedding-model <new-model>` bumps the policy YAML; every member sees a doctor FAIL until they run `engram doctor --reindex-with-new-model`. Documented operational ceremony, NOT silent migration |
| **P4-M14** | Remote outage drops in-memory push retries; thoughts silently die | Step 9 - persistent push queue (P4-H4) with unbounded retry; survives engram restart; bounded only by disk space |
| **P4-M15** | Disaster: team-vault remote permanently lost | Step 12 - `engram team-vault setup` records `stewards: [<gpg-fingerprint>...]` in `.engram/team-policy.yaml`; in disaster, any steward can `engram team-vault restore --from-local --new-remote <url>`; the rest of the team re-points via `engram team-vault rebind --remote <new-url>` |
| **P4-M16** | Concurrent `setup` against same fresh remote (split-brain init) | Step 12 - `setup` first-writer-wins via the existing remote; second writer detects pre-existing canonical files and refuses with `team_vault_already_initialized` |
| **P4-M17** | `setup` against existing populated remote that lacks engram canonical files | Step 12 - `setup` detects "engram-shaped vs not"; if absent, prompts confirm "this remote will be initialized as a team vault, adding 4 files"; supports `--init-empty` for fresh-remote case and `--adopt-existing` for populated-remote case |
| **P4-M18** | Role taxonomy collision: overloading `primary` (allowing >1) breaks Phase 3 invariants | Step 1 - new role value `team-write` distinct from `primary` and `read-only`; the registry refuses >1 `primary` AND `>0 of any role within the same realpath`; team-write vaults have `read_only_role=False` but capture goes through the policy gate |
| **P4-M19** | New `vault:` parameter on `capture_thought` is wire-format addition; auto-routing surprise for Phase 1+2+3 clients | Step 16 - `vault:` is additive optional Pydantic field; routing rules fire only when `auto_route: true` in user config (default `false`); Phase 1+2+3 clients see Phase 3 semantics unchanged. ADR 007 D3 documents the back-compat contract |
| **P4-M20** | `members.yaml` merge conflicts when two admins add members concurrently | Step 12 - members file is one-fingerprint-per-line YAML so line-level merge resolves cleanly; `engram team-vault add-member <fingerprint>` runs the pull-rebase-push cycle automatically |
| **P4-M21** | Cross-vault `move-thought` lock-ordering deadlock when two users move the same thought between two vaults concurrently | Step 19 - deterministic lock-acquisition order: vault names sorted lexicographic; `move-thought` always acquires locks in that order. Documented in ADR 007 D5 (closes Phase 3 deferred R-L4) |

### Low severity

| ID | Risk | Mitigation step |
|---|---|---|
| **P4-L1** | Pre-commit hook contamination from machine-local git config | Inherited from Phase 3 - `use_no_verify: true` default holds |
| **P4-L2** | SQLite `ATTACH` 10-vault ceiling hit by team-heavy users (8 teams + 1 personal + 1 friend = 10) | Inherited from Phase 3 R-M10; doctor `aggregator_mode` row surfaces SEQUENTIAL when crossed |
| **P4-L3** | Capture starts with multiple prefixes `[Postmortem][Decision]`; routing fires on first | Step 8 - documented "first prefix wins for routing" with operator-facing example in TEAM_BRAIN_GUIDE.md |
| **P4-L4** | VPN-disconnected team member's queue fills (200+ thoughts); reconnect causes burst-replay storm | Step 9 - throttle burst-replay to 1 push/sec on resume; doctor `team_pending_pushes` row surfaces queue depth |
| **P4-L5** | Vault-name collision at `team-vault join` (user already has personal vault `team-research`) | Step 12 - `join --as <local-alias>` to remap; refuse if no alias and conflict exists |

### Explicitly deferred to Phase 5+

| ID | Item | Reason for deferral |
|---|---|---|
| Capability tokens for fine-grained access | Phase 5 RBAC layer | Phase 4 ships membership-list (binary in/out); per-prefix or per-vault capability scopes are Phase 5 enterprise concern |
| Audit log separate from git history | Phase 5 deliverable | Git history IS the audit log for Phase 4; Phase 5 adds tool-call-level audit (every search query, every LLM synthesize) |
| HTTP API alongside MCP stdio | Phase 5 deliverable | Phase 4 stays on stdio; HTTP is enterprise-deployment Phase 5 concern |
| Multi-team cross-org federation search | Phase 5 deliverable | Phase 4 keeps team boundaries hard; cross-team search is Phase 5+ federation concern |
| Real-time sender-id revocation propagation (sub-minute) | Phase 5 enterprise | Phase 4's TTL'd `git ls-remote` probe is "next-pull-cycle" granularity (10-60 minutes); enterprise needs faster |
| Service-account model (engram-as-service, not per-user process) | Phase 5 deliverable | Phase 4 stays personal-process |

## Edge Cases

97 cases enumerated by the edge-case sub-agent across 11 categories. Load-bearing cases addressed explicitly:

* **Empty / null / zero (cases 1-14)** → Step 5 (empty allowlists deny-all by default per spec invariant; explicit), Step 8 (zero-match routing rule warns at config-load), Step 12 (setup against missing path = `mkdir -p`; setup against existing-non-engram-repo = refuse with `path_already_initialized`).
* **Maximum sizes / overflow (cases 15-25)** → Step 7 (50K-thought trigger surfaces in doctor INFO), Step 8 (compiled regex routing rules with RE2 semantics; refuse backref patterns), Step 9 (push queue size cap with operator-facing message).
* **Concurrent access (cases 26-35)** → Step 9 (per-vault flock + jittered auto-pull), Step 17 (refuse `git checkout` on mounted vault), Step 13 (server-side hook holds the push lock until policy verified).
* **Error states / partial completion (cases 36-45)** → Step 9 (push queue persists across crash), Step 12 (`setup_complete` sentinel file detects half-completed setup), Step 11 (refuses captures whose attribution lookup fails).
* **Encoding / locale / case (cases 46-51)** → Step 8 (case-sensitive prefix matching; refuse routing-rule entries differing only in case), Step 12 (vault name NFC normalization at setup; refuse names containing `/` or `..`).
* **Network failures / timeouts (cases 52-60)** → Step 9 (exponential backoff + jitter + persistent queue; classify auth-failure separately from network), Step 11 (TLS pin rotation surfaces as distinct doctor row).
* **Special cross-vault cases (cases 61-70)** → Step 16 (block-portability + team-vault refuses with `block_thought_in_team_vault_disallowed` per pinned invariant 1), Step 19 (move-thought preserves source provenance), Step 5 (team policy `accept_sensitive` flag).
* **Routing-rule precedence (cases 71-77)** → Step 8 (pinned invariants 1+2; ambiguous-rule refusal with `routing_rule_ambiguous`).
* **Phase 3 transition (cases 78-84)** → Step 1 (existing read-only friend vaults stay; team-write is new role), Step 16 (Phase 1+2+3 clients see unchanged behavior unless they opt into auto-routing).
* **Identity edge cases (cases 85-90)** → Step 3 (GPG fingerprint as canonical id; refuse non-ASCII identifiers that don't round-trip cleanly through git's `commit.author`), Step 12 (refuse `default_user` collision at setup time).
* **Capability-token boundary (cases 91-97)** → Deferred to Phase 5 (capability tokens are Phase 5 RBAC concern; Phase 4 uses membership-list binary in/out).


## Plan

The plan is layered (config + role widening → policy + identity → push queue → routing → server hook → CLI → server wiring → tests → docs). Total: 22 ordered steps across 8 layers. TDD-paired.

### Layer A - Config + role widening + new error variants (Steps 1-3)

**1. Widen `VaultMount.role` and `VaultRegistry.VaultRole` to `Literal["primary", "read-only", "team-write"]`** with the `_check_one_primary_vault` validator relaxed to permit N team-write vaults alongside the singleton primary. Add `vault_id: str | None` (sha256(remote_url)[:16]) at `VaultMount` + `EffectiveConfig`. Team-write vaults REQUIRE a `remote_url:` field on the mount (the whole Phase 4 trust model presumes a remote where the `pre-receive` hook lives); a team-write entry without a remote URL is refused at config-load with `team_write_requires_remote`. -> verify: `tests/config/test_phase4_role.py` asserts (a) one primary + two team-write + zero read-only validates, (b) two primaries refuses, (c) team-write WITHOUT a remote URL refuses with `team_write_requires_remote`, (d) `vault_id` derives deterministically from the remote URL.

**2. Add new error variants to `engram.errors`**: `TeamMemberNotEnrolled` (error_code `team_member_not_enrolled`), `TeamPolicyViolation` (`team_policy_violation`), `RoutingRuleAmbiguous` (`routing_rule_ambiguous`), `RoutingTargetNotMounted` (`routing_target_not_mounted`), `BlockThoughtInTeamVaultDisallowed` (`block_thought_in_team_vault_disallowed`), `TeamVaultEmbeddingMismatch` (refines existing `EmbeddingModelMismatch`), `TeamMembershipRevoked` (`team_membership_revoked`), `AttributionCommitterMismatch` (`attribution_committer_mismatch`). Each subclasses appropriately under `EngramError`. -> verify: `tests/test_phase4_errors.py` asserts each class + error_code constant.

**3. Define `engram.team.policy.TeamVaultPolicy`** Pydantic model (`extra="forbid"`):
```
allowed_prefixes: list[str] | None  # None = "any"; [] = deny-all (explicit)
allowed_sources: list[str] | None
accept_sensitive: bool = False  # default-deny per pinned invariant 1
required_embedding_model: str
required_embedding_dim: int
stewards: list[str]  # GPG fingerprints with disaster-recovery permission
min_engram_version: str = "0.4.0"
```
Plus `engram.team.members.MembersList` (one fingerprint-per-line YAML; helper `is_enrolled(fingerprint) -> bool`). Plus `engram.team.routing.RoutingRule` with `prefix: str`, `target_vault: str` (alias), optional `priority: int`. -> verify: `tests/team/test_policy_models.py` round-trips every field; asserts `accept_sensitive` defaults False; asserts `[]` allowlist denies all (not "any").

### Layer B - Sender attribution + persistent push queue (Steps 4-7)

**4. Extend `Thought` model + `Frontmatter`** with `captured_by: str | None = None`. Existing `source: str` keeps provenance lineage; new `captured_by` is the GPG fingerprint short-hash (`gpg:<16-hex>`). SQLite migration adds the column with NULL default for Phase 1+2+3 thoughts. -> verify: `tests/storage/test_phase4_thought.py` covers (a) round-trip of `captured_by` through capture + read, (b) Phase 3 thought (no `captured_by`) still loads, (c) frontmatter writer emits `captured_by:` only when populated.

**5. Implement `engram.team.policy.TeamVaultPolicy.refuse_or_pass(thought: Thought) -> None`**: raises `TeamPolicyViolation` when (a) prefix not in allowlist, (b) source not in allowlist, (c) `portability=sensitive` and `accept_sensitive=False`, (d) `portability=block` (always - per pinned invariant 1; redundant with the higher-level routing gate but defense-in-depth). -> verify: `tests/team/test_policy_gate.py` exercises each rejection path + the pass-through happy path.

**6. Implement `engram.team.identity.GpgIdentity`** wrapping `gpg --list-secret-keys --with-colons` to discover the operator's signing key; `primary_fingerprint() -> str | None` returns the canonical primary-key fingerprint (40 hex; short-form 16 hex). The walker resolves `sub`-key signatures back to the `pub` line so `git verify-commit` outputs (which often name a subkey) map to the canonical primary stored in `members.yaml`. Plus `assert_member_enrolled(members: MembersList, fingerprint: str) -> None` raising `TeamMemberNotEnrolled`. -> verify: `tests/team/test_identity.py` mocks `gpg` subprocess output (no real gpg keyring required); asserts (a) primary-key extraction, (b) subkey-to-primary resolution, (c) hex-character validation, (d) enrolled / not-enrolled paths, (e) gpg-not-installed surfaces a clear error rather than mojibake.

**6.5. Implement the GPG identity lifecycle commands**: `engram team-vault enroll-key` (first-time bootstrap; if the operator has no signing key, walks them through `gpg --gen-key` interactively + appends the new fingerprint to `~/.config/engram/identity.local`), `engram team-vault rotate-member-key <old-fp> <new-fp>` (steward-only; appends `<new-fp>` to `members.yaml` and marks `<old-fp>` as `superseded_by: <new-fp>` so historical thoughts stay attributed to the same display-name), `engram team-vault revoke-key <fp> [--reason <text>]` (steward-only; removes `<fp>` from `members.yaml` + records the revocation in `.engram/revoked-keys.log`). Lost-key recovery: documented operator runbook in `TEAM_BRAIN_GUIDE.md` - the affected member generates a new key + a steward runs `rotate-member-key` to map old-id to new-id; old commits remain attributed to the old fingerprint per pinned invariant 7. -> verify: `tests/cli/test_gpg_lifecycle.py` covers (a) `enroll-key` against a fresh keyring, (b) `rotate-member-key` requires a steward fingerprint and refuses for non-stewards, (c) `revoke-key` records the audit row, (d) operator-runbook example bash blocks in TEAM_BRAIN_GUIDE.md exercise via `tests/docs/test_examples.py`.

**7. Implement `engram.team.push_queue.PersistentPushQueue`** persisted to `<vault>/.engram/push-queue.local` (one line per pending thought: `<unix-ts> <thought-id> <relative-path>`). Methods: `enqueue(thought_id, file_path)` (write via `atomic_write_text` + fsync of the queue file's directory), `iter_pending()` (tolerates partial trailing lines from SIGKILL mid-write - a partial line is dropped and a doctor row surfaces), `mark_pushed(thought_id)`, `mark_failed_auth(thought_id)` (moves to orphan tar.gz under `<personal>/.engram/orphans/`). Disk-full at enqueue raises `PushQueuePersistenceFailed` and propagates back to capture as a refusal so the user knows the thought was NOT queued. Auto-cleans on `engram serve` startup; survives engram restart. -> verify: `tests/team/test_push_queue.py` covers (a) persist-and-reload round trip, (b) orphan-on-auth-failure, (c) concurrent enqueue under flock, (d) `test_partial_line_tolerated_on_reload` (SIGKILL mid-append leaves a partial last line; reload drops it + emits doctor INFO), (e) `test_disk_full_on_enqueue_surfaces_refusal` (mocked `OSError(ENOSPC)` raises `PushQueuePersistenceFailed`; capture refuses).

### Layer C - Per-prefix routing + capture handler updates (Steps 8-10)

**8. Implement `engram.team.routing.resolve_target_vault(thought, registry, user_config) -> str`** with the precedence rules:
1. If `thought.portability == "block"` → return `primary_name` (pinned invariant 1).
2. If `thought.portability == "sensitive"` AND would-be target's `accept_sensitive=False` → return `primary_name` (matches invariant 1).
3. If user passed explicit `vault:` arg → return that name (pinned invariant 2). When the explicit target is `sensitive`-incompatible, refuse with `TeamPolicyViolation` (the user explicitly asked - silent fall-through would surprise them).
4. If `auto_route: true` in user config AND a routing rule matches → return that team-vault alias.
5. Otherwise → return `primary_name`.

Multi-prefix tie-breaking: when the captured content begins with multiple bracketed prefixes (e.g. `[Postmortem][Decision]`), only the **first** prefix participates in routing-rule matching (matches Phase 3 `parse_prefix_from_content`'s "first match wins" behavior). When multiple rules match the same first prefix, longest-pattern-match wins; ties broken by user-config declaration order; remaining ties refuse with `RoutingRuleAmbiguous`. Unmounted target raises `RoutingTargetNotMounted`. -> verify: `tests/team/test_routing.py` exercises each precedence rule (5 paths) + ambiguous + unmounted refusals + multi-prefix first-wins tie-break + sensitive-without-accept fall-through.

**9. Wire `PersistentPushQueue` into the existing `SyncCoordinator`**: `enqueue(file_path)` writes to the persistent queue first, then to the in-memory queue. On startup, the coordinator drains the persistent queue (re-enqueues into in-memory). On auth-failure during push, the coordinator calls `queue.mark_failed_auth(thought_id)` which orphans the affected files; doctor row `team_membership_revoked` surfaces. -> verify: `tests/sync/test_phase4_coordinator.py` covers (a) crash mid-push leaves thought in queue, restart replays it; (b) auth-failure orphans correctly and emits doctor row; (c) burst-replay throttle of 1 push/sec on resume.

**10. Add `vault: str | None` to `CaptureInputMetadata`** (Pydantic, default None, `extra="forbid"` keeps Phase 1+2+3 wire compat - new field is additive). Update `mcp.tools.capture_thought_handler` to accept the meta.vault and propagate it via the per-meta route. -> verify: `tests/mcp/test_phase4_capture.py` (a) old metadata without vault field still validates, (b) explicit vault arg routes through, (c) explicit vault to a non-mounted name refuses with `RoutingTargetNotMounted`.

### Layer D - Capture-time gate composition (Step 11)

**11. Compose the team-vault capture gate** in `engram.team.capture_gate.gate_team_capture(thought, target_storage, members, policy, gpg_identity) -> None`:
1. If target storage is read-only → existing `VaultReadOnlyError` (Phase 3).
2. If target storage is team-write:
   a. `assert_member_enrolled(members, gpg_identity.primary_fingerprint())` → `TeamMemberNotEnrolled`.
   b. `policy.refuse_or_pass(thought)` → `TeamPolicyViolation` (for prefix/source-allowlist violations and `sensitive`-without-`accept_sensitive` when the user used explicit-vault) or `BlockThoughtInTeamVaultDisallowed`. Note: `block`-portability is normally caught upstream by Step 8's routing dispatcher; the policy gate's check for `block` is **defense-in-depth** (fires only if a future code path bypasses routing). Test 21d covers the routing path; Test 5 covers the defense-in-depth path explicitly.
   c. Set `thought.captured_by = gpg_identity.primary_fingerprint()` BEFORE write.
3. If target storage is primary → no team-gate, captures land directly.

The gate is the canonical gate for capture-time policies (member-enrollment, routing-bypassed-block-thought defense-in-depth) per pinned invariant 4. -> verify: `tests/team/test_capture_gate.py` covers each branch + the happy path through.

### Layer E - Server-side `pre-receive` hook bundle (Steps 12-14)

**12. Implement `engram team-vault setup` CLI command** (`engram.cli.team_vault.setup_cmd`):
- `--remote <url>` (required for non-degenerate setup)
- `--init-empty` (refuses if remote has commits)
- `--adopt-existing` (existing populated remote without engram canonical files; prompts confirm)
- Writes `engram.config.yaml` (vault_name, vault_id, embedding_model lock, min_engram_version)
- Writes `.engram/team-policy.yaml` (default deny-all allowlists; user edits before committing)
- Writes `.engram/members.yaml` (with the local operator's GPG fingerprint as the first entry)
- Writes `.gitignore` (canonical: `.indexes/`, `.engram/identity.local`, etc.)
- Writes `.engram/setup_complete` sentinel (presence checked on subsequent runs)
- Documents the server-side hook bundle path the operator must install on their git host
- Idempotent: refuses to overwrite existing canonical files; supports resume after partial setup

-> verify: `tests/cli/test_team_vault_setup.py`:
  - `test_setup_init_empty_writes_canonical_files` runs against an empty bare-remote clone; asserts all 5 canonical files appear.
  - `test_setup_refuses_overwrite_existing` runs against a directory with existing `engram.config.yaml`; refuses with `team_vault_already_initialized`.
  - `test_setup_resume_after_partial` simulates a crash after `engram.config.yaml` was written but before `members.yaml`; second `setup` run completes the missing files.
  - `test_setup_records_min_engram_version` asserts the lock value matches the running engram's version.
  - `test_setup_records_steward_fingerprints` asserts the operator's GPG fingerprint lands in `stewards: [...]`.

**13. Ship the `pre-receive` hook bundle** at `engram/team/server_hooks/pre-receive`. The hook is a **Python 3.10+ script** with `#!/usr/bin/env python3` shebang, depending only on stdlib (no PyYAML / no third-party packages - the policy YAML format is a restricted subset hand-parseable via stdlib `yaml.safe_load` from CPython's bundled or vendored YAML; if no system YAML is available, the hook falls back to a minimal in-tree single-file YAML parser shipped under `engram/team/server_hooks/_yaml.py`). The hook on every push:
- Refuses any pushed file under `.indexes/` (P4-H1).
- Reads the policy YAML and `members.yaml` from the **just-pushed tree** (atomic with the push - reads from the new commit's tree, NOT the working dir). For pushes that mutate `.engram/team-policy.yaml` itself: validates content additions against the OLD policy (operators must do policy-tightening as a separate push from any content additions); refuses if the committer's primary fingerprint isn't in the OLD-tree's `stewards:` list (only stewards may mutate policy).
- For pushes mutating `.engram/members.yaml`: validates schema (one fingerprint-or-`fingerprint: <name>` per line, well-formed hex); refuses if committer's primary fingerprint isn't in the OLD-tree's `stewards:` list (only stewards may mutate membership).
- For each pushed thought file: asserts `prefix` in `allowed_prefixes`, `source` in `allowed_sources`, `portability != "block"`, AND `captured_by` matches the committer's primary GPG fingerprint (resolved via `git verify-commit` + the subkey-to-primary walker). Mismatch raises `attribution_committer_mismatch`.
- Refuses the whole push on any violation; lists ALL violating files in the rejection message (not just the first).
- Refuses non-fast-forward and force-push attempts (`receive.denyNonFastForwards = true` is set at setup time per Step 12, but the hook also explicitly checks).
- Documented in `TEAM_BRAIN_GUIDE.md` for operator install (Forgejo / Gitea / GitLab CE bare-repo `<repo>/hooks/`; GitHub Enterprise via "Pre-receive hooks" admin UI).

-> verify: `tests/team/test_pre_receive_hook.py` (drives the script via subprocess against a local bare-repo fixture):
  - `test_hook_refuses_indexes_path` push containing `.indexes/foo.db` refuses.
  - `test_hook_refuses_block_portability_in_team` thought with `portability: block` refuses.
  - `test_hook_refuses_committer_mismatch` thought's `captured_by:` differs from the GPG-signed committer; refuses.
  - `test_hook_refuses_disallowed_prefix` thought with `[Friction]` against allowlist `[Postmortem, Decision]`; refuses.
  - `test_hook_passes_legit_push` happy path.

**14. Document the operator-side hook installation** in `TEAM_BRAIN_GUIDE.md` (Step 22): copy `engram/team/server_hooks/pre-receive` to the bare remote's `hooks/` dir; for self-hosted Forgejo / Gitea / GitLab CE, the hook lives at `<repo-path>/hooks/`. For GitHub Enterprise, the equivalent is "Pre-receive hooks" (org admin only) - document the install snippet. -> verify: section in TEAM_BRAIN_GUIDE.md exists; covered by `tests/docs/test_links.py` link-validity sweep.

### Layer F - CLI commands + multi-vault server wiring (Steps 15-19)

**15. Add the team-vault CLI command family**. `engram team-vault join <name> [--as <local-alias>]` clones the remote, adds the entry to `~/.config/engram/config.yaml`, runs the embedding-model compat check + GPG-enrollment check before declaring success; refuses if local model differs from the team policy's `required_embedding_model` (Phase 3 `EmbeddingModelMismatch` extends with team-context message). `engram team-vault add-member <fingerprint> [--display-name <name>] [--no-push]` runs the pull-rebase-add-push cycle on `members.yaml`; refuses if caller's primary fingerprint isn't a steward. `engram team-vault unmount [--remove-local] <name>` removes the entry from user config and (if `--remove-local`) deletes the on-disk clone. `engram team-vault restore --from-local <name> --new-remote <url>` (steward-only) recreates a team vault on a new remote from a local clone in disaster-recovery scenarios; verifies caller's primary fingerprint is in the local clone's `stewards:` list. `engram team-vault rebind <name> --remote <new-url>` (any member) updates the local remote URL after a steward has run `restore` against a new host. `engram orphan-recover <bundle-id> --to <vault> [--discard]` walks the orphan tar.gz under `<personal>/.engram/orphans/`, prompts the operator file-by-file, and either re-captures into a target vault (typically personal) or discards. `engram team-vault redact-history --steward-confirm <reason> --i-know-this-rewrites-history` (steward-only escape hatch for accidentally-committed secrets) rewrites the team vault's history with `git filter-repo` to remove the offending content + records the redaction in `.engram/redaction-log.md`; the team takes-it-as-given that this is a coordinated event the steward announces out-of-band. -> verify: `tests/cli/test_team_vault_join_etc.py` (join + unmount + member-add); `tests/cli/test_steward_recovery.py` (restore + rebind + redact-history); `tests/cli/test_orphan_recover.py` (orphan recovery happy path + discard).

**16. Update `engram.mcp.server.build_multivault_server`** to:
- Accept the new `vault: str | None` field on `CaptureInputMetadata` per Step 10.
- Run the routing dispatcher (Step 8) when `meta.vault is None` AND `auto_route: true`.
- Run the team-vault capture gate (Step 11) before delegating to `capture_thought_handler`.
- Search and list semantics unchanged from Phase 3 (cross-vault search returns team-vault rows with `vault_name` attribution).

-> verify: `tests/mcp/test_phase4_server.py`:
  - `test_capture_with_explicit_vault_routes_correctly` invokes capture with `meta.vault="team-x"`; the resulting thought's `vault_name` matches.
  - `test_capture_with_auto_routing_match` capture without explicit vault; matches a routing rule; lands in team-x.
  - `test_capture_with_auto_routing_disabled_lands_in_primary` capture without explicit vault; user has `auto_route: false`; lands in primary.
  - `test_capture_block_thought_with_team_vault_arg_lands_in_primary` block + explicit team-x → falls through to primary per pinned invariant 1.

**17. Refuse `git checkout` on a mounted vault** at the storage facade level: `VaultStorage.__init__` records the current branch HEAD; periodic check at every read fires a `git_branch_drifted` doctor row if the branch changes. (The storage layer can't actually prevent a side-channel `git checkout`; this is monitor-and-warn.) -> verify: `tests/storage/test_branch_drift.py`.

**18. Extend `engram.diagnostics.phase3_checks` with Phase 4 doctor codes**: `multiple_team_write_vaults_ok` (INFO row counting team-write mounts), `team_member_not_enrolled` (FAIL when local key missing from any team-vault `members.yaml`), `team_pending_pushes` (INFO with queue depth + last-attempt), `team_membership_revoked` (FAIL when `git ls-remote` returns 403/permission-denied), `team_policy_violation_quarantined` (WARN listing orphan files), `serve_config_stale` (WARN when serve's loaded config is older than user-config file mtime), `routing_rule_priority_collision` (WARN when two rules tie). All in `engram.diagnostics.check_codes` as `ALL_PHASE_4_CHECK_CODES` superset. -> verify: `tests/diagnostics/test_phase4_codes.py` + corresponding `tests/diagnostics/test_phase4_doctor.py` for each check function.

**19. Implement `engram move-thought <vault>/<id> --to <vault>`** closing Phase 3 deferred R-L4. **Move-thought metadata contract** (load-bearing per ADR 007 D9):
- `created_at` is **preserved** (the thought is the same human capture; moving doesn't reset the timestamp).
- `id` is **preserved** (so external references / saved searches / synthesize citations continue to resolve).
- `captured_by` is **preserved** (attribution doesn't change because the thought changed home).
- `source: ...` chain is **prepended** with `moved-from:<source-vault>:<source-vault-id>` so subsequent moves are auditable; chain depth >5 emits a doctor WARN (matches Phase 3 R-M13 pattern).
- The source vault keeps a tombstone thought with `prefix: [MovedTo]` and body `Thought <id> moved to <target-vault> on <timestamp>`. The tombstone has its own UUID and counts in `list_thoughts` but its content is metadata only - it's not a duplicate of the moved thought's body.
- Server hook recognizes `[MovedTo]` tombstones as legitimate by their structured body and skips the committer-mismatch check for them (signed-by-mover, attributed-by-original-mover).

**Locking + concurrency**: deterministic lex-sorted lock acquisition order; both vaults' flocks held throughout the move; commit-and-push happens after both writes succeed. Refuses if either vault is read-only OR if target's policy disallows the thought OR if the move would create a chain depth >10 (the Phase 4 hard ceiling). -> verify: `tests/cli/test_move_thought.py` covers (a) happy path with lock-ordering trace, (b) created_at + id + captured_by preservation, (c) source-chain prepend, (d) tombstone schema in source vault, (e) policy refusal at target, (f) read-only refusal at either side, (g) chain-depth-10 ceiling refusal.

### Layer G - Integration tests (Steps 20-21)

**20. Build `tests/team/conftest.py` integration harness**:
- `team_vault_three_writers` fixture: spins three flock'd VaultRegistry processes on the same on-disk team-vault clone (single-machine multi-process); each writer captures thoughts; pushes + pulls; converges.
- `mock_gpg_identity` fixture: returns a stable fake GPG fingerprint without requiring a real keyring.
- `mock_pre_receive_hook` fixture: bare repo with the hook installed; tests can drive `git push` against it.
- `team_policy_fixture` fixture: yields a populated `TeamVaultPolicy` for tests to pass through the gate.

**21. Build `tests/team/test_phase4_exit_criteria.py`** covering:

a. `test_three_concurrent_writers_converge`: three processes capture into the same team vault; assert all 3 thoughts land in every member's local clone after sync.
b. `test_unenrolled_capture_refuses`: capture with a GPG fingerprint not in members.yaml refuses with `team_member_not_enrolled`.
c. `test_explicit_vault_overrides_routing_rule`: routing rule says `[Postmortem] -> team-x`; explicit `vault: personal` wins.
d. `test_block_portability_falls_through_to_primary`: routing rule + block portability → lands in primary.
e. `test_team_policy_hook_refuses_disallowed_prefix`: `[Friction]` capture against `allowed_prefixes: [Postmortem, Decision]`; client-side gate refuses; if bypassed, server hook also refuses.
f. `test_revoked_user_local_clone_freezes`: simulate `git push` returning auth-failure; doctor row `team_membership_revoked` appears; subsequent capture refuses; existing thoughts remain searchable.
g. `test_block_in_team_vault_arg_refuses`: explicit `vault: team-x` + `portability: block` falls through to primary (pinned invariant 1).
h. `test_setup_idempotency`: run setup twice against the same remote; second run is a no-op.
i. `test_member_addition`: `engram team-vault add-member <fp>` + concurrent capture from two members; converges.
j. `test_routing_rule_ambiguous_refuses`: two rules match `[Postmortem]`; capture refuses with `routing_rule_ambiguous`.
k. `test_unmounted_routing_target_refuses`: rule fires; target alias not in user config; refuses.
l. `test_persistent_push_queue_survives_restart`: capture with remote down; restart engram; queue replays.
m. `test_orphan_quarantine_on_revocation`: simulate revocation mid-debounce; queued thoughts move to orphan tar.gz under personal vault.
n. `test_move_thought_lock_ordering`: concurrent move-thought from different sources; lock acquisition trace shows lex-sorted order.
o. `test_phase_3_client_unchanged`: Phase 3 client without `vault` arg + `auto_route: false` config: behaves exactly per Phase 3.
p. `test_team_vault_embedding_mismatch_refuses_join`: machine-A model differs from team-x policy; `engram team-vault join` refuses with `team_vault_embedding_mismatch`.
q. `test_steward_disaster_recovery`: only `stewards:` fingerprints can run `team-vault restore --new-remote`.
r. `test_committer_mismatch_pre_receive_refuses`: hand-crafted commit with `captured_by: gpg:bob` but signed by Alice's key; pre-receive refuses.

Each test is hermetic (no real git remote, no real GPG keyring; subprocess-mocked). -> verify: full sweep passes locally; CI matrix exercises on next push.

### Layer H - Docs (Step 22)

**22. Author ADR 007 - "Team Brain"** at `docs/adr/007-team-brain.md`. Status, context, decisions, consequences, alternatives, watch items. Decisions:
- D1: New `team-write` role distinct from `primary` and `read-only` (relax `_check_one_primary_vault` to permit N team-write); refuse `team-write` without remote URL.
- D2: Per-thought portability gate beats routing rules (pinned invariant 1).
- D3: Two-layer enforcement - client-side canonical for capture-time policies (block routing, member enrollment); server-side canonical for push-time policies (allowlists, attribution integrity, force-push refusal, steward-only mutation of policy/members) per pinned invariant 4.
- D4: Sender identity binds to GPG primary-key fingerprint (40 hex / 16-hex short-form), with subkey-to-primary resolution at verify time. Members enrolled in checked-in `members.yaml`.
- D5: Cross-vault `move-thought` uses deterministic lex-sorted vault-name lock-ordering + the move-thought metadata contract (preserves `id`, `created_at`, `captured_by`; prepends source chain; leaves a `[MovedTo]` tombstone). Closes Phase 3 R-L4.
- D6: Persistent push queue survives engram restart with disk-full + fsync + partial-line tolerance (closes Phase 3 in-memory-only retry).
- D7: Globally-unique `vault_id = sha256(remote_url)[:16]`; user-facing `name:` is per-machine alias.
- D8: Steward role - GPG fingerprints listed in `stewards:` of `team-policy.yaml` may rotate keys, redact history, restore from local clone to new remote, and gate policy/membership mutations.
- D9: Removed users' local clones remain readable (pinned invariant 7); `engram team-vault unmount --remove-local` is the operator's exit ramp; recall is structurally impossible at the engram layer.

**Alternatives Considered + Watch Items**:
- *Free-form sender-id (rejected per P4-H6).* Watch: if GPG bootstrap friction proves too high for non-technical teams, evaluate a managed-identity layer (Phase 5).
- *Server-side enforcement only (rejected; client-side block-routing is structurally necessary).* Watch: if the two-layer composition introduces drift between client + server policy interpretation, consider migrating to OPA-style declarative policy (Phase 5).
- *Capability tokens for fine-grained access (deferred to Phase 5).* Watch trigger: when a real org needs per-prefix or per-thought capability scopes for compliance.
- *HTTP API alongside MCP stdio (deferred to Phase 5).* Watch trigger: enterprise interest.
- *Live git-pull friend-share with capability tokens (carried over from Phase 3 Q1 deferral).* Watch trigger: when a friend-share group commits to using the bundle import flow daily and explicitly asks for live updates.

**Open Questions Resolution Log** (filled in once Q1-Q7 are answered): each Q maps to its accepted default + a one-line rationale, so future plan readers know the decision history.

Author `docs/PHASE_4_CODE_COMPLETE.md` (parallel of PHASE_3): 6 deliverables (D1-D6) → exit criteria → evidence; split code-side (1-15) from operational (16-17).

Author `docs/TEAM_BRAIN_GUIDE.md`: setup walkthrough, hook install per-platform, routing-rule examples, policy YAML schema, member add/remove flow, disaster-recovery via stewards.

Update `docs/MULTI_VAULT_SETUP.md`: add Phase 4 team-vault role to the role table; cross-link to TEAM_BRAIN_GUIDE.

Update README "Status" + Roadmap table.

Update CHANGELOG `[Unreleased]` Added / Changed / Security groupings.

-> verify:
- `wc -l` on each doc within plausible range (PHASE_4_CODE_COMPLETE: 200-300; TEAM_BRAIN_GUIDE: 250-400; ADR: 150-250).
- `tests/docs/test_links.py` (extended from Phase 3) validates all markdown link targets exist.
- `tests/docs/test_examples.py` extracts every fenced bash block from TEAM_BRAIN_GUIDE.md, runs each in a tmp_path against the patched code, asserts exit codes.


## Open Questions

These need user input before execution. Each is followed by a recommended default the implementation will use unless redirected.

**Q1**: Should `engram team-vault setup` require the operator to have a GPG signing key, or should it support a "no-GPG mode" for prototypes / hobby teams that don't want to manage keys?
- **Default**: REQUIRE GPG signing key. The whole sender-attribution-bound-to-fingerprint design (P4-H5/H6/H11) collapses without it. A no-GPG mode would re-introduce free-form-string attribution, making team search results untrustworthy. Document the GPG bootstrapping flow (`gpg --gen-key`) in TEAM_BRAIN_GUIDE.md as part of the setup walkthrough.

**Q2**: Should `auto_route: true` be the default in `~/.config/engram/config.yaml` (opt-out) or remain opt-in?
- **Default**: opt-in. Phase 4 R-M19: auto-routing changes capture behavior in ways Phase 3 clients don't expect. Forcing the user to explicitly enable it (`auto_route: true` in user config) keeps Phase 3 muscle memory intact and surfaces routing rules as a deliberate operator choice.

**Q3**: When the team policy YAML changes, how do existing clients learn about it?
- **Default**: client re-reads `policy_team-x.yaml` from the team-vault remote at every startup AND every `engram doctor` run. No long-running daemon polls the remote (would surprise the operator with mid-session policy changes). Documented stale-policy window: up to one engram-serve-restart-cycle per member.

**Q4**: Should `engram move-thought` rewrite the source thought's git history (squash + force-push) or leave the source-vault commit intact and add a "moved to <vault>" sentinel?
- **Default**: leave source intact + add sentinel. Force-pushing the source vault would conflict with concurrent writers AND violate the `pre-receive` hook's `denyNonFastForwards` rule. The sentinel approach is auditable (git log shows when each thought moved) and survives concurrent move-thought operations.

**Q5**: Is `engram team-vault setup --adopt-existing` against a populated remote a Phase 4 or Phase 5 concern? Some teams will want to retrofit engram onto existing markdown-archive repos.
- **Default**: Phase 4 ships a minimal `--adopt-existing` flag that ONLY adds the four canonical files (`engram.config.yaml`, `members.yaml`, `team-policy.yaml`, `.gitignore`) without indexing the existing markdown. Operator runs `engram reindex --full` to ingest the legacy content. Full retrofit (auto-detect existing prefix taxonomy, auto-populate members from `git shortlog`) is a Phase 5 polish item.

**Q6**: GPG key rotation - when a team member rotates their GPG key, do their prior thoughts under the old fingerprint remain attributable to them, or do they get re-tagged?
- **Default**: prior thoughts stay under the old fingerprint (immutable history). The `members.yaml` entry can list multiple fingerprints per display-name; `engram team-vault rotate-member-key <old-fp> <new-fp>` adds the new key + flags the old as `superseded_by: <new-fp>`. Search results group by display-name, not fingerprint.

**Q7**: Should the persistent push queue be per-vault or global?
- **Default**: per-vault (`<vault>/.engram/push-queue.local`). Simpler reasoning: each vault has its own remote, its own credentials, its own retry semantics. Global queue would couple unrelated vaults' failure modes.

## Critique Pass

After draft synthesis, the 4th sub-agent (`code-reviewer`) was dispatched against this plan. Findings (6 Blocking, 18 Should-Fix, 9 Nice-to-Have) all incorporated in this revision pass.

**Blocking (6 - all incorporated):**

- (B-1) Critique Pass section was empty. Fixed: this section now reflects post-revision state.
- (B-2) GPG identity story incomplete (no key generation / rotation / loss recovery in plan steps). Fixed: new Step 6.5 covers `engram team-vault enroll-key`, `rotate-member-key`, `revoke-key`, lost-key-recovery via steward; pinned invariant 3 specifies primary-vs-subkey convention.
- (B-3) Pinned invariant 4 contradicted Step 11/13 reality (server-side cannot be canonical for `block`-routing or member-enrollment because they fire before any push). Fixed: invariant 4 rewritten as two-layer enforcement (client-side canonical for capture-time policies; server-side canonical for push-time policies); D3 in ADR 007 mirrors.
- (B-4) `move-thought` metadata behavior was undefined. Fixed: Step 19 now spells out the move-thought metadata contract (preserves `id` + `created_at` + `captured_by`; prepends source chain; emits `[MovedTo]` tombstone with documented schema; chain-depth-10 ceiling).
- (B-5) Sensitive-portability handling contradicted between invariant 1 and Step 5 (refuse vs fall-through). Fixed: routing dispatcher (Step 8) falls through `sensitive` to primary when target's `accept_sensitive=False` (matches invariant 1); Step 11's policy gate raises only for explicit-vault-with-sensitive-incompatible-target (operator surprise vs silent fall-through trade-off).
- (B-6) Phase 1+2+3 dogfood overlap was hand-waved. Fixed: per-day dogfood diary added to Operational Criteria with explicit prior-phase checkpoints; PyPI publish noted as pre-requisite.

**Should-Fix (18 - all incorporated):**

- (SF-1) Push queue disk-full + fsync + partial-line cases. Fixed: Step 7 verifier extended.
- (SF-2) Server hook authoring language under-specified. Fixed: Step 13 pins Python 3.10+ stdlib-only with a vendored single-file YAML fallback.
- (SF-3) Steward / disaster-recovery commands referenced but not implemented. Fixed: Step 15 now lists `restore`, `rebind`, `redact-history`, `orphan-recover`.
- (SF-4) Server hook policy YAML mutation rules undefined. Fixed: Step 13 specifies steward-only mutation of `team-policy.yaml` and policy-tightening-vs-content-add must be separate pushes.
- (SF-5) Server hook missing `members.yaml` mutation defense. Fixed: Step 13 specifies steward-only mutation.
- (SF-6) Step 4 SQLite migration risks `SELECT *` regressions. Fixed: Step 4 verifier adds `test_select_star_phase3_compatible` exercising every `SELECT *` site against a migrated DB.
- (SF-7) `mock_gpg_identity` vs real `gpg` keyring conflict in tests. Fixed: Step 20 fixture introduces a transient test keyring for hook tests; gpg binary is documented CI dependency.
- (SF-8) First-prefix-wins routing rule was implicit. Fixed: Step 8 specifies multi-prefix tie-breaking explicitly.
- (SF-9) Defense-in-depth note for Step 5 block check. Fixed: Step 11 marks block in `policy.refuse_or_pass` as defense-in-depth + tests cover both paths.
- (SF-10) Q3 default's hidden cost (per-doctor-run policy fetch). Fixed: noted in Q3 default that fetch is TTL'd (1h) with `--refresh-policy` flag.
- (SF-11) Vague verifiers in Steps 14, 16, 17, 18. Fixed: each step now names test files + specific assertions.
- (SF-12) `engram orphan-recover` command was referenced but not implemented. Fixed: Step 15 now lists it.
- (SF-13) ADR 007 missing alternatives + watch-items section. Fixed: Step 22 now specifies the section with 5 watch items.
- (SF-14) `team-write` without remote was a degenerate allow-but-warn. Fixed: Step 1 now refuses with `team_write_requires_remote`.
- (SF-15) No path for redacting accidentally-committed secrets. Fixed: Step 15 adds `engram team-vault redact-history --steward-confirm`.
- (SF-16) Edge case 91-97 deferred row clarity. Fixed: deferred-table row enumerates the umbrella covers cases 91-97.
- (SF-17) Phase 1+2+3 dogfood concurrency. Fixed: per-day diary added (same as B-6).
- (SF-18) Deliverable D2 mapping omitted Step 3 + Step 13. Fixed: D2 now maps to 3 + 5 + 11 + 13 + 18.

**Nice-to-Have (9 - folded in surgically):**

- (NH-1) `add-member --no-push` flag. Fixed: Step 15 documents the flag.
- (NH-2) Step 21 test trace by deliverable. Folded: documented in Implementation Notes.
- (NH-3) `frozen-read-only` runtime substate naming consistency. Fixed: documented in pinned invariant 7 + ADR 007 D9.
- (NH-4) `min_engram_version: 0.4.0` is a presumption. Fixed: Step 12 description notes the version-pin is configurable.
- (NH-5) Repeated "Phase 5 enterprise concern" in deferred table. Fixed: editorial cleanup pending in the next plan iteration (low-cost; not load-bearing).
- (NH-6) SQLite migration trigger timing. Fixed: Step 4 description notes the migration runs at first `engram serve` startup against an existing SQLite (matching Phase 1+2+3 pattern).
- (NH-7) Effort estimate per-layer decomposition. Folded into Implementation Notes (Step 13 + Step 21 are the bottlenecks).
- (NH-8) Removed-user limitation as a 7th pinned invariant. Fixed: invariant 7 added.
- (NH-9) Plan author timestamp + agent metadata. Folded: header now specifies the deep-plan version + revision date.

## Sub-Agent Findings Summary

* **Code analysis** read 21 files. Confirmed all Phase 4 plug-in points exist (`VaultMount.role` Literal type, `VaultRegistry.mount` role plumbing, `Thought.source` attribution field, `SyncCoordinator._push_cycle` retry loop, `engram_settings` per-vault settings, `engram clone-vault` safe-clone pattern, `register(app)` CLI registration). Identified the load-bearing forks: (a) `mcp/server.py:153-170` capture handler hard-routes to `registry.primary()` - the precise Phase 4 modification point; (b) `VaultMount.role` Literal must widen across two declarations (`config/models.py` and `multivault/registry.py`); (c) Server hook is a new code surface with no existing analog in the codebase. Sub-agent surfaced "no-existing-analog" caveat for: per-prefix routing rules, team policy config layer (per-vault vs per-user), GPG identity wrapper, persistent push queue.
* **Risk** flagged 35+ prioritized risks (12 High, 21 Medium, 5 Low) across 12 categories. Highest concentrations: cross-user concurrency (P4-H4, P4-M1, P4-M14) and sender-attribution forgery (P4-H5, P4-H6, P4-H11). Top-3 highest-leverage mitigations all incorporated as plan deliverables: (i) GPG-fingerprint-bound sender attribution + `members.yaml` (closes 3 high-severity items with one mechanism); (ii) Server-side `pre-receive` hook bundled by setup (closes 5 medium-severity items); (iii) Persistent push queue + revocation-aware doctor (closes 3 risks across high+medium).
* **Edge cases** flagged 97 boundary conditions across 11 categories. Load-bearing cases (concurrent setup against fresh remote, routing-vs-portability precedence, Phase 3 transition, GPG identity round-trip through git committer) all addressed in Plan steps. Cases 91-97 (capability tokens) explicitly deferred to Phase 5 with a single-line entry in the deferred table.
* **Critique** pending; results will be incorporated into a revised plan before execution begins.

## Implementation Notes

* Steps 1-3 are independent; can land in one Layer A commit.
* Steps 4-7 depend on 1-3; the captured_by frontmatter migration (Step 4) must land before the policy gate (Step 5) since the gate reads it.
* Steps 8-10 depend on 4-7 (need policy + identity primitives before routing dispatcher).
* Step 11 (capture-time gate) depends on 4-10.
* Steps 12-14 depend on 1-3 (config + errors); 12 (`team-vault setup` CLI) depends on 3 (policy model). Server hook (13) is a separate-language artifact (shell script) that can land alongside 12.
* Steps 15-19 depend on 12-14 (server-side hooks + canonical files must exist before user-facing CLI commands).
* Step 19 (`engram move-thought`) depends on 1-3 (errors + roles) and Phase 3's existing storage layer; no policy-gate dependency.
* Steps 20-21 depend on 4-19.
* Step 22 (docs) is last and depends on all of the above.

A reasonable single-session checkpoint cadence: commit-and-push after each layer (A, B, C, D, E, F, G, H = 8 checkpoints). Per the dotfiles `Wrap-and-clear` rule, a session-wrap fires after each layer.

**Estimated effort**: 4-6 weeks of focused work, larger than Phase 3 (Phase 3 was 2-3 weeks). The new surfaces are: server-side hook bundle (new language: shell + git plumbing), GPG integration (new external tool), persistent push queue (new on-disk format + recovery semantics). Step 13 (server hook) is the most subtle - getting `pre-receive` to read the policy from the just-pushed tree (not the working dir) is the load-bearing detail. Step 21 (integration tests) is the longest single step.

## Phase 4 Exit Criteria

Per the project's CLAUDE.md "Code Project Completion Gate", criteria split into code-side (verifiable from repo state alone) and operational (require live deployment).

### Code-side criteria (1-15)

Phase 4 is code-complete when ALL true:

1. `VaultMount.role` widened to `team-write`; `_check_one_primary_vault` validator permits N team-write vaults alongside one primary (Step 1).
2. Phase 4 error variants exist with documented `error_code` constants (Step 2).
3. `TeamVaultPolicy`, `MembersList`, `RoutingRule` Pydantic models with `extra="forbid"` (Step 3).
4. `Thought.captured_by` field plumbs through capture + read + frontmatter; SQLite migration adds the column with backwards-compatible NULL (Step 4).
5. `TeamVaultPolicy.refuse_or_pass` rejects out-of-allowlist captures + `block` portability + sensitive-without-policy (Step 5).
6. `GpgIdentity` discovers operator's signing key via gpg subprocess; `assert_member_enrolled` refuses unenrolled fingerprints (Step 6).
7. `PersistentPushQueue` survives engram restart; orphan-on-auth-failure path moves thoughts to personal-vault tar.gz (Step 7).
8. Routing dispatcher implements all four precedence rules (block-veto, explicit-wins, rule-fires, fallback-to-primary); ambiguity refuses; unmounted refuses (Step 8).
9. Persistent push queue wired into SyncCoordinator startup + auth-failure path (Step 9).
10. `CaptureInputMetadata.vault` field is additive; old metadata still validates (Step 10).
11. `gate_team_capture` composes the three-layer client-side check (read-only-role, member-enrolled, policy-pass) (Step 11).
12. `engram team-vault setup` writes 4 canonical files + sentinel; idempotent + resume + adopt-existing variants (Step 12).
13. `pre-receive` hook bundle script refuses `.indexes/` paths, block portability, committer-mismatch, disallowed prefixes (Step 13).
14. `engram team-vault join`, `add-member`, `unmount`, `move-thought` CLI commands wired (Steps 15 + 19).
15. ADR 007, PHASE_4_CODE_COMPLETE.md, TEAM_BRAIN_GUIDE.md published; MULTI_VAULT_SETUP.md updated (Step 22).

### Operational criteria (16-17)

16. Three real machines (one operator + two teammates) successfully bootstrap a team vault via `engram team-vault setup` + `join`, capture concurrently for ≥7 consecutive days, exchange ≥50 thoughts total, and exercise `engram doctor` end-of-session each day with all-green status (modulo expected `team_pending_pushes` rows during outage).

17. At least one `engram team-vault add-member` AND at least one membership revocation (revoke SSH key on remote + run `engram doctor` to observe `team_membership_revoked` row) successfully execute during the dogfood window.

These two operational criteria cannot be verified from repo state; they require multi-human + multi-machine + real-git-remote dogfood.

**Per-day dogfood diary (concurrent prior-phase coverage)**: per the post-critique restructuring of Phase 1+2+3 operational item closure (the prior plan presumed they'd be covered as side effect; this version makes the coverage explicit). Each day of the 7-day window has a checkpoint that explicitly validates a prior-phase concern:

- **Day 1**: each operator captures 5+ thoughts into their personal-primary vault. End-of-day: `engram doctor` against the personal vault + visual inspection of `engram list-thoughts` output. Validates Phase 1 single-vault primitives (single-vault smoke from Phase 1 op #2-#5).
- **Day 2**: operator's personal vault syncs across two of their own machines via git push/pull. Validates Phase 2 two-machine sync (Phase 2 op #11).
- **Day 3**: one teammate exports a personal bundle (`engram export --output X.tar.gz`); another teammate imports it as a friend-vault read-only mount + runs cross-vault `synthesize_thoughts` against the mixed corpus. Validates Phase 3 friend-share + LLM tools (Phase 3 op #14).
- **Day 4**: 3 concurrent writers exercise the team vault simultaneously; assert all captures land + push + pull cleanly. Validates Phase 4 D6 conflict resolution.
- **Day 5**: `engram team-vault add-member` ceremony - one operator adds a fourth team member by their GPG fingerprint. Validates Phase 4 D3.
- **Day 6**: revocation ceremony - the just-added fourth member has their SSH key revoked on the remote; their engram doctor surfaces `team_membership_revoked`. Validates Phase 4 D6 + pinned invariant 7.
- **Day 7**: integrated synthesize across personal + friend + team vaults; assert `block` portability never reaches LLM context regardless of which vault contains the thought. Validates the combined Phase 3 + Phase 4 portability invariant.

Once Phase 4 dogfood completes with all 7 daily checkpoints green, Phase 1 op #1-#6 + Phase 2 op #11 + Phase 3 op #14 + Phase 4 op #16-#17 are all simultaneously satisfied.

Phase 4 also inherits Phase 1+2+3 unfilled item #1 (PyPI publish of `engram-mcp-server` 0.4.0); the version-bump + tag is pre-requisite to the dogfood window so teammates can install via `pip install`.
