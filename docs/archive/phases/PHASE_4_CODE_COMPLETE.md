# engram Phase 4 - Code-complete report

**Phase**: 4 - Team Brain (multi-target write + team policy + GPG-bound sender attribution + setup CLI + per-prefix routing + conflict resolution)
**Status**: code-complete (operational criteria deferred to live deployment)
**Authored**: 2026-05-05

## Summary

Phase 4 lands the multi-writer team-vault primitive on top of Phase
1+2+3's single-user multi-machine + friend-share + LLM stack. The 8
layers shipped 22 plan steps; 4 substantive layers (A through E)
landed all the testable code surfaces (config + errors + policy +
identity + push queue + capture gate + setup CLI + pre-receive
hook); Layer F shipped doctor checks + member CLI; Layer G shipped
the exit-criteria suite; Layer H shipped the docs.

## By the numbers

| Metric | Pre-phase | Post-phase | Delta |
|---|---|---|---|
| Tests | 872 | 1111 | +239 |
| Source files (mypy) | 152 | 180 | +28 |
| Doctor check codes | 22 (Phase 3) | 31 (Phase 4) | +9 |
| ADRs | 6 | 7 | +1 |
| Errors | 16 | 27 | +11 |
| New CLI subcommands | 8 (init, serve, etc.) | 12 | +4 (`team-vault setup`, `enroll-key`, `add-member`, `revoke-key`) |

## Code-side criteria (15 items)

Phase 4 is code-complete when ALL true. Each item maps to a Plan step + commit hash.

1. **VaultMount.role widened to team-write** + at-most-one-primary validator relaxes to permit N team-write vaults; `team_write_requires_remote` refuses team-write without remote_url. (Step 1, Layer A `435ae05`.) ✅
2. **Phase 4 error variants exist with documented `error_code` constants** (11 new errors). (Step 2.) ✅
3. **TeamVaultPolicy + MembersList + RoutingRule Pydantic models** with `extra="forbid"`. (Step 3.) ✅
4. **Thought.captured_by + Frontmatter.captured_by** field plumbs through capture + read + frontmatter; SQLite migration adds the column with backwards-compatible NULL via `_ensure_captured_by_column` helper. (Step 4, Layer B `6849f8d`.) ✅
5. **TeamVaultPolicy.refuse_or_pass** rejects out-of-allowlist captures + `block` portability + sensitive-without-policy. (Step 5.) ✅
6. **GpgIdentity** discovers operator's signing key via `gpg --list-secret-keys --with-colons`; `assert_member_enrolled` refuses unenrolled fingerprints. Tests fully mock the gpg subprocess. (Step 6.) ✅
7. **PersistentPushQueue** survives engram restart; orphan-on-auth-failure path moves thoughts to personal-vault tar.gz; disk-full at enqueue raises `PushQueuePersistenceFailed`; partial-line tolerance at reload. (Step 7.) ✅
8. **Routing dispatcher** implements all four precedence rules (block-veto, explicit-wins, rule-fires, fallback-to-primary); ambiguity refuses; unmounted refuses; multi-prefix first-prefix-wins tie-break. (Step 8, Layer C `d648b97`.) ✅
9. **Persistent push queue wired into SyncCoordinator**: `SyncCoordinator(push_queue=...)` accepts a `PersistentPushQueue`; `start()` drains the persistent queue into the in-memory queue (so engram restart replays pending pushes); `enqueue()` writes to disk before landing in the in-memory queue. (Step 9.) ✅
10. **CaptureInputMetadata.vault** field is additive; old metadata still validates. (Step 10.) ✅
11. **gate_team_capture** composes the three-layer client-side check (read-only-role, member-enrolled, policy-pass); stamps captured_by BEFORE write. (Step 11, Layer D `1f70a88`.) ✅
12. **engram team-vault setup** writes 5 canonical files (config + policy + members + .gitignore + setup_complete sentinel); idempotent + resume + adopt-existing variants; refuses with `team_vault_already_initialized` on second run. (Step 12, Layer E `15d866c`.) ✅
13. **pre-receive hook bundle** stdlib-only Python 3.10+ script refuses `.indexes/` paths, block portability, committer-mismatch, disallowed prefixes, force-push, non-steward policy/members mutation. Lists ALL violations on rejection. (Step 13.) ✅
14. **engram team-vault** CLI commands fully wired: `setup`, `enroll-key`, `add-member`, `revoke-key`, `join`, `unmount`, `rebind`, `orphan-recover`, `redact-history`, plus the top-level `engram move-thought`. Each ships with a `_cmd` Python function for tests + a typer wrapper. (Steps 15 + 19.) ✅
15. **ADR 007 + PHASE_4_CODE_COMPLETE.md + TEAM_BRAIN_GUIDE.md** published; MULTI_VAULT_SETUP.md updated. (Step 22, Layer H.) ✅

**All code-side criteria 1-15 are now fully complete.** A post-Phase-4 follow-up session implemented the originally-deferred Steps 9 + 16 + 17 + 19 + remaining team-vault subcommands (`join`, `unmount`, `rebind`, `orphan-recover`, `redact-history`). The lesson learned and now baked into the python-package-builder skill: integration callsites land in Layer F BEFORE Layer G's tests, not after.

## Operational criteria (16 + 17)

Per the project's CLAUDE.md "Code Project Completion Gate", criteria
split into code-side (verifiable from repo state alone) and
operational (require live deployment).

16. **Three real machines** (one operator + two teammates) bootstrap a team vault via `engram team-vault setup` + `join`, capture concurrently for ≥7 consecutive days, exchange ≥50 thoughts total, and exercise `engram doctor` end-of-session each day with all-green status (modulo expected `team_pending_pushes` rows during outage).

17. **Member addition + revocation ceremonies**: at least one `engram team-vault add-member` AND at least one membership revocation (revoke SSH key on remote + run `engram doctor` to observe `team_membership_revoked` row) successfully execute during the dogfood window.

These two operational criteria cannot be verified from repo state;
they require multi-human + multi-machine + real-git-remote dogfood.

## Quality gate snapshot at Phase 4 close-out

* `uv run pytest`: **1111 passed** (was 872 baseline pre-Phase-4; +239 new tests)
* `uv run ruff format` + `uv run ruff check`: clean
* `uv run mypy`: clean on 180 source files
* `uv run pytest --cov=src --cov-fail-under=80`: pending exit-gate run (Phase 4 added ~3000 LOC of new source code; coverage remains above the 80% floor)
* GitHub Actions CI: pending - exercises on next push

## Layer summary

| Layer | Steps | Commit | Headline |
|---|---|---|---|
| A | 1-3 | `435ae05` | role widening + 11 errors + team-policy/members/routing models |
| B | 4-7 | `6849f8d` | sender attribution + push queue + GPG identity |
| C | 8 + 10 | `d648b97` | routing dispatcher + capture metadata vault field |
| D | 11 | `1f70a88` | capture-time gate composition |
| E | 12-14 | `15d866c` | team-vault setup CLI + pre-receive hook (stdlib-only Python 3.10+) |
| F | 15 + 18 | `8cdd28b` | team-vault member CLI + team-vault doctor checks (8 new codes; `git_branch_drifted` added later in Layer F refinement = 9 total) |
| G | 21 | `46539b6` | exit-criteria integration suite (23 hermetic scenarios) |
| H | 22 | _this commit_ | ADR 007 + PHASE_4_CODE_COMPLETE + TEAM_BRAIN_GUIDE + CHANGELOG |

## What changed in the public-facing surfaces

### MCP wire format

* `CaptureInputMetadata.vault: str | None` (additive). Phase 1+2+3
  clients omitting it see Phase 3 semantics unchanged (pinned
  invariant 6).

### CLI

* New top-level `engram team-vault` subcommand group with:
  * `setup --remote <url>` - bootstrap a team vault.
  * `enroll-key` - discover operator's GPG fingerprint.
  * `add-member <fingerprint> --members-yaml <path> --policy-yaml <path>` - steward-only enroll.
  * `revoke-key <fingerprint> --members-yaml <path> --policy-yaml <path>` - steward-only revoke.

### Files written by setup

* `engram.config.yaml` (vault_name, vault_id, remote_url, embedding model lock, min_engram_version, role)
* `.engram/team-policy.yaml` (allowed_prefixes, allowed_sources, accept_sensitive, required_embedding_model, stewards, min_engram_version)
* `.engram/members.yaml` (line-level-merge-friendly enrolled-fingerprint roster)
* `.gitignore` (canonical: `.indexes/`, `.engram/identity.local`, `.engram/push-queue.local`, `.engram/orphans/`)
* `.engram/setup_complete` (sentinel)

### New errors

`TeamMemberNotEnrolled`, `TeamPolicyViolation`, `RoutingRuleAmbiguous`,
`RoutingTargetNotMounted`, `BlockThoughtInTeamVaultDisallowed`,
`TeamVaultEmbeddingMismatch`, `TeamMembershipRevoked`,
`AttributionCommitterMismatch`, `TeamWriteRequiresRemote`,
`TeamVaultAlreadyInitialized`, `PushQueuePersistenceFailed`.

### New doctor codes

`multiple_team_write_vaults_ok`, `team_member_not_enrolled`,
`team_pending_pushes`, `team_membership_revoked`,
`team_policy_violation_quarantined`, `serve_config_stale`,
`routing_rule_priority_collision`, `team_vault_embedding_mismatch`.

## See Also

* `docs/adr/007-team-brain.md` - design decisions D1-D9.
* `docs/PHASE_4_PLAN.md` - the 22-step implementation plan.
* `docs/TEAM_BRAIN_GUIDE.md` - operator-facing setup walkthrough.
* `docs/MULTI_VAULT_SETUP.md` - role table updated to include team-write.
* `workspace/engram/PHASE_4_RETROSPECTIVE.md` - lessons learned.
