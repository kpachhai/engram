# ADR 005 - Sync coordinator state machine + cross-vault contamination guard

**Status**: accepted (Phase 2)
**Date**: 2026-05-05
**Supersedes**: extends ADR 003 (system-git CLI sync model)
**Implementation**: `engram.sync.coordinator`, `engram.sync.identity`, `engram.sync.startup_probes`

## Context

ADR 003 fixed the high-level sync transport: engram uses the system git
CLI, never a library, and the algorithm is per-vault clone-and-pull. It
left two questions open:

1. **How does the coordinator decide WHEN to commit + push?** Auto on every
   capture risks thousands of single-thought commits; auto on a long timer
   risks losing thoughts to a crash. The coordinator must be a state machine
   so its decisions are auditable and its transitions are testable.

2. **How does engram defend against pushing personal thoughts to a work
   remote (or vice versa)?** Without a guard, a misconfigured remote URL
   silently leaks one vault's content into another's history.

Phase 2 also added the cross-cutting risks R-H1 through R-H10 + R-M1
through R-M15 from `docs/PHASE_2_PLAN.md`. Several of those (R-M9
force-push gap, R-H6 conflict markers, R-H7 cloud-sync corruption) need
explicit decisions captured here.

## Decision

### State machine

The coordinator owns ten explicit states encoded as `enum.StrEnum`:

| State | Meaning |
|---|---|
| `idle` | Nothing pending; clean rest. |
| `debouncing` | Captures queued; debounce timer armed. |
| `committing` | Actively running `git commit`. |
| `committed_not_pushed` | Local commit succeeded; push failed for a non-retry reason. Resume on next tick or on startup. |
| `fetching` | Running `git fetch` as part of the non-fast-forward recovery path. |
| `pushing` | Actively running `git push`. |
| `paused_for_migration` | `MigrationLock` held by `engram migrate-from-open-brain`; coordinator parks. |
| `auth_required` | AUTH-classified failure; never auto-retry. Operator must reconcile credentials. |
| `manual_resolution_required` | Terminal error (force-push gap, rebase conflict, unknown classification). Operator runs `engram sync --resume` after manual git work. |
| `disabled` | `sync.disabled=true`; coordinator runs no loop. |

Allowed forward transitions are encoded in `ALLOWED_TRANSITIONS`. Any
disallowed transition raises `engram.errors.SyncError` rather than
silently advancing. A separate `allow_from_any` escape hatch exists for
crash-handling paths (`MANUAL_RESOLUTION_REQUIRED` is reachable from
anywhere when the loop crashes).

### Force-push gap (R-M9 reflog gate)

Before `git pull --rebase` runs as part of the non-fast-forward recovery,
the coordinator MUST:

1. Capture the previous `origin/<branch>` SHA via `git rev-parse`
2. Run `git fetch`
3. Assert the previous SHA is reachable from the new `origin/<branch>`
   via `git merge-base --is-ancestor <prev_sha> origin/<branch>`

If the previous SHA is unreachable, an upstream force-push has rewritten
history past our local view. The coordinator transitions to
`manual_resolution_required` and does NOT attempt rebase. The operator
must intervene; this prevents silent loss of local commits to a remote
history rewrite.

### Cross-vault contamination guard (R-H3)

Each vault carries a machine-local file at `<vault>/.engram/identity.local`
(gitignored, NOT committed) with two required fields:

```yaml
vault_id: kpachhai-personal
expected_remote_pattern: '^git@github.com:kpachhai/.*-personal\.git$'
```

`engram serve` startup probe 11 (`vault_identity_remote_match`) reads the
file, resolves `git remote get-url origin`, and refuses to start if the
URL does not match `expected_remote_pattern`. The probe also re-runs
before every push so a mid-session admin change (`git remote set-url`)
cannot leak. A missing identity file is a WARN (not FAIL); identity is
opt-in, but is the only way to enforce contamination prevention.

### Force semantics

- Coordinator NEVER invokes `git push --force`.
- Coordinator MAY invoke `git push --force-with-lease` ONLY after the
  reflog-gate succeeded AND a clean rebase landed.
- `--force` is reserved for operator-initiated work outside engram.

### Conflict markers (R-H6)

`engram serve` scans `thoughts/*.md` at startup for both `<<<<<<<` AND
`>>>>>>>` markers (paired). If found, it FAILs and refuses to serve:
indexing a markdown file with conflict markers would corrupt the YAML
frontmatter and produce a Pydantic strict-parse error. The hunk
separator `=======` alone is a markdown horizontal-rule and is NOT a
trigger.

### Cloud-sync refusal (R-H7)

The same probe set FAILs at startup if `.git/` resolves under a known
cloud-sync directory (Dropbox, iCloud Drive, Google Drive, OneDrive,
Box Sync, pCloud, MEGA). SQLite + `.git/` semantics on these are
unreliable (SQLite WAL truncation collides with the provider's atomic
upload model). Operators who want multi-machine sync use git-based sync
(this Phase 2 deliverable) with a non-synced vault directory.

## Consequences

### Positive

- Every state transition is auditable via the in-memory ring buffer
  (last 256 events) and via state transitions surfaced in
  `engram doctor` output.
- The force-push gap test is a single unit test; the security property is
  defended without operator vigilance.
- Read-only role (`sync.role=read-only`) is enforced at three layers:
  - Probe 14 FAILs `role=read-only AND auto_push_on_capture=true`.
  - `_push_cycle` skips immediately when `role=read-only`.
  - `engram sync --push` returns `vault_read_only` exit code.

### Negative

- The state machine adds ~600 LOC vs a flat
  "commit-then-push-then-handle-errors" function. Justified because the
  flat version's invariants are not testable without deterministic state
  capture; the state machine's invariants are testable as
  enum-transition properties.
- The reflog gate adds one extra `git rev-parse` per push attempt. The
  cost is microseconds; the recovery path runs only on
  non-fast-forward.

### Operator burden

- Each vault MUST have `.gitignore` that includes `.indexes/` and
  `*.sqlite*` patterns (probe 8 FAILs otherwise).
- Each vault SHOULD have `.engram/identity.local` for cross-vault
  contamination defense (WARN if missing).
- `engram clone-vault` is the recommended way to clone vault remotes
  because it deletes `.git/hooks/` BEFORE checkout (R-H1).

## Alternatives considered

### "Just call git pull/push directly without a state machine"

Rejected: state transitions cannot be reliably audited from log lines
alone, and recovery paths (`committed_not_pushed`, `auth_required`)
require persistent state across ticks that a flat function cannot
express without the same machinery anyway.

### "Use a git library (pygit2, dulwich) instead of system git"

Rejected per ADR 003 (system git only). Phase 2 does not relitigate.

### "Sign every commit by default"

Rejected per Q2 default. Most users do not have GPG infrastructure on
day one; making it required would block adoption. `signed_pull_required`
is opt-in, with the doctor probe surfacing a WARN when it is on but the
trusted-keys file is missing.

### "Auto-resolve conflicts via merge driver"

Partially adopted via `merge.engram-thoughts.driver=cat -` for
append-only files (R-H4 mitigation). Concurrent edits to the SAME
thought file are documented as a known limitation; the maintainer's
discipline is to avoid editing the same thought from two machines
simultaneously. Phase 3 multi-vault isolation reduces blast radius.

## References

- `docs/PHASE_2_PLAN.md` - the full 21-step implementation plan with
  risk + edge-case tables.
- `docs/MULTI_MACHINE_SETUP.md` - operator-facing setup guide.
- `src/engram/sync/coordinator.py` - reference implementation.
- ADR 003 - system git CLI sync transport.
