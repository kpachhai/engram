# ADR 007 - Team Brain (multi-target write + GPG-bound attribution + per-prefix routing + server-side hook)

**Status**: Accepted
**Date**: 2026-05-05
**Phase**: 4
**Supersedes**: none
**Superseded-by**: none

## Context

Phase 1+2+3 made engram a personal memory system with multi-machine
sync (Phase 2) and one-way friend-share via point-in-time bundles
(Phase 3). Phase 4 introduces the Team Brain: a small group of users
(2-12 people) capturing into the SAME team vault concurrently, with
trustworthy sender attribution, push-time policy enforcement, and a
clear revocation story.

The team-vault model is a structural extension of Phase 3's
multi-vault primitives. The new role `team-write` joins `primary` and
`read-only`. Where `primary` is "the singleton vault this engram
captures into by default" and `read-only` is "this vault is observable
but no writes accepted", `team-write` is "this vault accepts writes
but only after passing a capture-time client-side gate AND a push-time
server-side hook".

Three classes of risk shape the design:

1. **Attribution forgery.** A free-form `default_user` string (the
   Phase 1+2+3 attribution mechanism) is too weak when multiple users
   share a vault: anyone can `git config user.email alice@example.com`
   and produce thoughts attributed to Alice. Phase 4 binds attribution
   to the operator's GPG primary signing key fingerprint.
2. **Cross-machine index pollution.** A team-vault user fat-fingering
   `git add -A` from the vault root would commit `.indexes/` SQLite
   files to the team remote, clobbering every other member's local
   index on every pull. Phase 4 bundles a `.gitignore` AND a
   server-side `pre-receive` hook that refuses any push containing
   `.indexes/` paths.
3. **Removed-user recall impossibility.** Once a user has a local
   clone, the markdown files exist on their disk. We cannot recall
   them at the engram layer; the contract is "revocation prevents
   future writes + reads against the live remote, but pre-existing
   distributed clones remain readable". Phase 4 documents this as a
   structural property, not a bug.

## Decisions

### D1. New `team-write` role distinct from `primary` and `read-only`

`VaultMount.role` widens to `Literal["primary", "read-only",
"team-write"]`. The validator `_check_one_primary_vault` relaxes to
permit N `team-write` vaults alongside the singleton primary. A
`team-write` vault REQUIRES `remote_url`; refused at config-load with
`team_write_requires_remote` if absent.

**Why a new role rather than overloading `primary`**: overloading
breaks the at-most-one-primary invariant from Phase 3 R-M9 + the LLM
resolver's "primary takes the LLM if no per-vault override" semantics
break ambiguously. A new role is additive; Phase 1+2+3 callers see
the same `primary` semantics.

### D2. Per-thought portability gate beats routing rules (pinned invariant 1)

`portability=block` ALWAYS lands in personal-primary regardless of
explicit `vault:` arg, per-prefix routing rules, or team policy.
`sensitive` follows routing only when the destination team-vault's
policy declares `accept_sensitive: true`. `portable` follows the
routing chain unrestricted.

**Why**: the portability classification is the user's hard contract
about where a thought may travel. Routing is a convenience layer; it
never overrides the hard contract.

### D3. Two-layer enforcement (pinned invariant 4)

Client-side is canonical for **capture-time** policies:
`block`-portability routing, member-enrollment refusal. These fire
BEFORE any disk write or push attempt; the server hook can only
inspect what reaches it.

Server-side is canonical for **push-time** policies: prefix allowlist,
source allowlist, `captured_by`-vs-git-committer integrity,
`.indexes/` refusal, force-push refusal, `members.yaml` /
`team-policy.yaml` steward-only mutation.

The two layers compose: a client-side bypass (older client, forked
engram, hand-edited markdown) doesn't breach the boundary because
the server-side hook catches it.

### D4. Sender identity binds to GPG primary-key fingerprint (pinned invariant 3)

The canonical sender id is the operator's GPG primary signing key
fingerprint (40 hex). Members are enrolled in `members.yaml` checked
into the team remote. Capture into a `team-write` vault refuses with
`team_member_not_enrolled` if the local key isn't enrolled.

**Subkey resolution**: `git verify-commit` may surface a subkey
fingerprint when the commit was signed by a signing subkey. The hook
resolves subkeys back to the primary via `gpg --with-colons`; the
canonical id stored in `members.yaml` is always the primary
fingerprint.

**Subkey rotation does NOT require `members.yaml` mutation**;
primary-key rotation does and is a steward-gated operation
(`engram team-vault rotate-member-key`).

### D5. Cross-vault `move-thought` metadata contract

`engram move-thought <vault>/<id> --to <vault>` preserves `id`,
`created_at`, and `captured_by` (the thought is the same human
capture; moving doesn't reset the timestamp or attribution). The
`source` chain is prepended with `moved-from:<source-vault>:<source-vault-id>`
so subsequent moves are auditable; chain depth >5 emits a doctor
WARN; >10 refuses.

The source vault keeps a `[MovedTo]` tombstone with body `Thought
<id> moved to <target-vault> on <timestamp>` so the source vault's
git history records the move.

**Locking**: deterministic lex-sorted lock acquisition order (vault
names sorted lexicographic) avoids deadlock under concurrent moves.

### D6. Persistent push queue survives engram restart (P4-H4 mitigation)

Pending pushes persist to `<vault>/.engram/push-queue.local` (one
line per pending thought). Disk-full at enqueue raises
`PushQueuePersistenceFailed` and propagates back to capture as a
refusal so the user knows the thought was NOT durably enqueued. Auth-
failure during push moves affected files to an orphan tarball under
`<personal>/.engram/orphans/` for the operator's `engram orphan-recover`
flow.

**Partial-line tolerance**: a SIGKILL mid-append leaves a partial
trailing line; reload drops it + emits a doctor INFO row.

### D7. Globally-unique `vault_id`; user-facing `name` is per-machine alias (pinned invariant 5)

`vault_id = sha256(remote_url)[:16]`. Two machines may use different
aliases for the same team vault; the registry indexes by id internally.
`engram team-vault join --as <local-alias>` lets the user pick a local
alias when their preferred name is already taken on this machine.

### D8. Steward role - GPG fingerprints in `team-policy.yaml`

Stewards may rotate keys, redact history, restore from local clone to
new remote, gate policy/membership mutations. The first operator
running `engram team-vault setup` becomes the first steward
automatically. Additional stewards are added by an existing steward
running `engram team-vault add-member --steward <fingerprint>`.

The server-side hook enforces steward-only mutation: a push that
modifies `.engram/team-policy.yaml` or `.engram/members.yaml`
refuses if the committer's primary fingerprint isn't in the OLD
tree's `stewards:` list.

### D9. Removed users' local clones remain readable (pinned invariant 7)

A user removed from the team has their git push capability revoked
at the remote AND their local team-vault mount auto-degrades to
`frozen-read-only` (cannot capture; LLM tools refuse against it;
doctor surfaces `team_membership_revoked`). But the on-disk markdown
clone exists and any operating-system-level read access creates
copies. `engram team-vault unmount --remove-local <name>` provides
the operator's exit ramp; the team takes-it-as-given that revocation
does not delete prior-distributed copies.

## Consequences

### Positive

* **Trustworthy attribution**: GPG-fingerprint-bound sender id makes
  team search results reliable. "Postmortems by alice" is verifiable.
* **Defense in depth**: two enforcement layers compose; a single
  bypass doesn't breach the boundary.
* **Backwards compatible**: Phase 1+2+3 clients without `vault:` arg
  + `auto_route: false` see Phase 3 behavior unchanged.
* **Operator visibility**: 8 new doctor codes surface team-vault
  health (pending pushes, missing enrollment, stale config, etc.).

### Negative

* **GPG bootstrap friction**: non-technical teams may struggle with
  `gpg --gen-key`. Documented walkthrough in TEAM_BRAIN_GUIDE.md;
  watch item: managed-identity layer in Phase 5.
* **Recall impossibility**: removed users keep their local clones.
  Documented in pinned invariant 7 + D9; the team takes this as
  given.
* **Server-hook authoring complexity**: the pre-receive hook is a
  novel code surface (Python 3.10+ stdlib-only, vendored YAML
  parser). Future Phase 5 may simplify with a managed forge layer.

## Alternatives Considered

### Free-form sender-id (rejected per P4-H6)

**Watch**: if GPG bootstrap friction proves too high for non-technical
teams, evaluate a managed-identity layer (Phase 5).

### Server-side enforcement only (rejected; client-side block-routing is structurally necessary)

**Watch**: if the two-layer composition introduces drift between
client + server policy interpretation, consider migrating to
OPA-style declarative policy (Phase 5).

### Capability tokens for fine-grained access (deferred to Phase 5)

**Watch trigger**: when a real org needs per-prefix or per-thought
capability scopes for compliance.

### HTTP API alongside MCP stdio (deferred to Phase 5)

**Watch trigger**: enterprise interest.

### Live git-pull friend-share with capability tokens (carried over from Phase 3 Q1 deferral)

**Watch trigger**: when a friend-share group commits to using the
bundle import flow daily and explicitly asks for live updates.

## Open Questions Resolution Log

| ID | Question | Default applied | Rationale |
|----|----------|-----------------|-----------|
| Q1 | GPG required at setup? | YES | Without it, attribution collapses. Document `gpg --gen-key` walkthrough in TEAM_BRAIN_GUIDE.md. |
| Q2 | `auto_route` default? | OFF (opt-in) | Phase 3 muscle memory + R-M19 surprise mitigation. |
| Q3 | Policy YAML refresh cadence? | Re-read at startup + every `engram doctor` (TTL'd to 1h via `--refresh-policy` flag). |
| Q4 | `move-thought` rewrite history? | NO (sentinel approach) | force-push conflicts with concurrent writers + violates pre-receive hook denyNonFastForwards. |
| Q5 | `--adopt-existing` Phase 4 or 5? | Phase 4 (minimal) | Adds 4 canonical files; full retrofit (auto-detect prefix taxonomy from existing markdown) is Phase 5. |
| Q6 | GPG key rotation behavior? | Prior thoughts stay under old fingerprint | Immutable history; `members.yaml` lists multiple fingerprints per display-name; `rotate-member-key` adds new + flags old as `superseded_by:`. |
| Q7 | Push queue per-vault or global? | Per-vault (`<vault>/.engram/push-queue.local`) | Each vault has its own remote/credentials/retry semantics; global queue would couple unrelated failure modes. |

## See Also

* `docs/archive/phases/PHASE_4_PLAN.md` - the implementation plan this ADR closes the loop on.
* `docs/archive/phases/PHASE_4_CODE_COMPLETE.md` - exit-criteria evidence.
* `docs/TEAM_BRAIN_GUIDE.md` - operator-facing setup walkthrough.
* `docs/adr/006-multi-vault-and-llm.md` - Phase 3 design rationale (the multi-vault primitives this Phase scales).
* `docs/adr/005-sync-coordinator.md` - Phase 2 sync coordinator state machine (Phase 4 scales to N writers per remote).
