# engram Phase 2 - Multi-machine Personal Sync (Implementation Plan)

**Authored**: 2026-05-05 via `superpowers:deep-plan` (3 parallel sub-agents + critique)
**Spec sources**:
- `docs/superpowers/specs/2026-05-04-engram/03-ROADMAP.md` Phase 2 (deliverables 1-7 + exit criteria, lines 64-95)
- `docs/superpowers/specs/2026-05-04-engram/02-TECHNICAL_DESIGN.md` Component C5 (sync coordinator), Flow C (git env), Flow D (debounced commit/push), Vault Isolation
- `docs/adr/003-sync-model.md` (system git CLI; no library)
- `06-SECURITY.md` Boundary B3 (privacy boundary via separate remotes)

## Goal

When complete, two machines running `engram serve` against separate clones of the same git remote converge on the same set of thoughts within one debounce window of capture, with conflict events surfaced (not auto-resolved) and a read-only role flag preventing the work machine from ever pushing.

Verifier: integration test `tests/sync/test_two_machine_convergence.py` runs two `VaultStorage` instances against a single `git init --bare` remote, captures on A, asserts B sees the thought after `engram sync --pull`, and asserts a forced-push from elsewhere triggers degraded mode on the next pull.

## Current State

**Already built in Phase 1:**

* `VaultStorage.capture()` calls `self._post_capture_sync(thought)` (storage/facade.py:269-274) - stub `del thought` no-op, explicitly named "Phase 2+ wiring point" in code + ADR 003.
* `engram.utils.run_command.run_git()` is fully implemented (no callers yet): always prepends `"git"`, requires explicit `cwd`, 30s default timeout, pre-stages all four Flow C env vars (`GIT_TERMINAL_PROMPT=0`, `GIT_MERGE_AUTOEDIT=no`, `GIT_ASKPASS=true`, `GIT_LFS_SKIP_SMUDGE=1`), supports `extra_env` merge.
* `VaultLock` (utils/lock.py) uses `fcntl.flock LOCK_EX|LOCK_NB`; signal/atexit cleanup; cross-host vs same-host detection. Sync coordinator runs **inside** an already-held lock during `engram serve`.
* `SyncConfig` (config/models.py:24-34) already has six Phase 2 fields shipped: `auto_pull_on_startup`, `auto_commit_on_capture`, `auto_push_on_capture`, `git_remote`, `git_branch`, `startup_pull_timeout_seconds`. `extra="forbid"`. Composes into `VaultConfig.sync` and `EffectiveConfig.sync`.
* CLI structure: `register(app)` pattern in `engram.cli.__init__`. New subcommands attach by adding a sibling module + import line + `register(app)` call.
* Doctor: `CheckResult` / `CheckStatus` (OK/WARN/FAIL) / `DoctorReport` framework with `report.add(name, status, message, detail)`; new sync checks attach as `_check_*` functions called inside `run_diagnostics()`.

**What Phase 2 builds:**

A `engram.sync.coordinator` module implementing a state-machine-driven asyncio coordinator that owns the post-capture queue, debounces commits, performs `git fetch`/pull/push with bounded retry, detects conflicts, and exposes a manual `engram sync` CLI. Plus eleven new doctor checks, an `.engram/identity.local` cross-vault contamination check, and integration tests using a `git init --bare` remote.

## Risks

Prioritized; each maps to a Plan step or to Open Questions.

### High severity

| ID | Risk | Mitigation step |
|---|---|---|
| **R-H1** | Cloned vault repo's `.git/hooks/` execute arbitrary code on next checkout | Step 14 - `engram clone-vault` helper does `git clone --no-checkout` then `rm -rf .git/hooks` then checkout |
| **R-H2** | Attacker-controlled remote injects content via `git pull` that engram indexes | Step 15 - signed-commit verification gate via `git verify-commit` against an allow-list in `~/.config/engram/trusted-keys.yaml`. **Off by default** (opt-in via `sync.signed_pull_required: true`); the as-shipped configuration accepts unsigned commits. Doctor WARNs when `signed_pull_required=true` but trusted-keys file missing (Step 13). |
| **R-H3** | Cross-vault contamination (personal ↔ work) via misconfigured remote | Step 5 + 11 - `vault.identity` field in non-versioned `.engram/identity.local`; coordinator refuses to push if `git remote get-url origin` does not match the expected URL pattern for the identity |
| **R-H4** | Lost-update on concurrent capture across machines | Step 8 - `git config merge.engram-thoughts.driver` set to `cat -` (union behaviour for append-only files); plus the existing UUID-v7 last-12-hex filename naming makes file collisions structurally near-impossible. Note: this addresses **new-thought** races; concurrent edits to the SAME existing thought are documented as M-8 below (different mitigation surface). |
| **R-H5** | Committed-not-pushed state lost on crash | Step 9 - state machine has `committed_not_pushed` state; startup probe runs `git rev-list @{u}..HEAD` and resumes push if local is ahead |
| **R-H6** | Conflict markers corrupt Pydantic strict-parsed frontmatter | Step 13 - new `_check_conflict_markers` doctor check; coordinator scans markdown for `<<<<<<<` markers on startup AND after every pull; if found, enter degraded mode (search OK, capture refused) per Flow C line 487 |
| **R-H7** | SQLite index + `.git/` corrupt under cloud-sync (Dropbox / iCloud) | Step 13 - new `_check_cloud_sync_under_dotgit` doctor check; FAIL (not WARN) if `.git/` is under a known cloud-sync root |
| **R-H8** | Unsigned commits when global `commit.gpgsign=true` is set, falling back is non-defensible | Step 11 - startup probe verifies gpg-agent reachability if signing required; coordinator refuses to fall back to unsigned without explicit `sync.allow_unsigned: true` |
| **R-H9** | `engram migrate-from-open-brain` running concurrently with serve sync loop | Step 12 - `MigrationLock` separate from `VaultLock`; sync coordinator pauses (`paused-for-migration` state) while migration in progress |
| **R-H10** | Two `engram serve` processes on different machines push concurrently | Step 9 + Step 8 - state-machine recovery: fetch → rebase → re-push with `--force-with-lease`, never `--force`; refuse if rebase produces conflicts |

### Medium severity

| ID | Risk | Mitigation step |
|---|---|---|
| **R-M1** | Thousands of single-thought commits from `auto_commit_on_capture` | Step 7 - debounce window default 60s, max coalesce 300s; new config field `sync.debounce_window_seconds` (default 60.0, gt=0) |
| **R-M2** | Default branch name divergence (master vs main) across machines | Step 11 - startup probe verifies `git symbolic-ref refs/remotes/origin/HEAD` matches `sync.git_branch`; refuse to push otherwise |
| **R-M3** | Line-ending mangling (`core.autocrlf`) | Step 11 - startup probe FAILs if `core.autocrlf != false` AND `.gitattributes` does not pin `*.md text eol=lf`; `engram init` writes the `.gitattributes` |
| **R-M4** | `pull.rebase` config disagreement across machines | Step 8 - coordinator always invokes `git pull --rebase=true` explicitly, never bare `git pull` |
| **R-M5** | SSH key not loaded in agent at engram startup | Step 9 - state machine classifies "Permission denied (publickey)" as non-retryable; transitions to `auth-required`; surfaces actionable error |
| **R-M6** | Token expiry on HTTPS remotes (401/403) | Step 9 - same as R-M5; parse stderr, classify, never retry |
| **R-M7** | Markdown rewrite by merge → fingerprint mismatch → forced reindex | **Already mitigated in Phase 1**: fingerprint is content-hash (SHA-256) per `engram.utils.fingerprint`, not mtime-based. Phase 2 verifies via integration test (Step 19) |
| **R-M8** | Silent merge of concurrent edits to same thought file | Step 13 + 19 - integration test surfaces this as a known limitation; doctor + ADR-005 document trade-off (markdown-as-SoT means user discipline required for cross-machine edits); Phase 3 multi-vault isolation reduces blast radius |
| **R-M9** | Force-push from elsewhere wipes local unpushed commits | Step 9 sub-step 9b - explicit reflog check: before rebase, capture previous `origin/<branch>` SHA; after fetch, assert previous SHA is reachable from new `origin/<branch>` via `git merge-base --is-ancestor`; refuse to auto-rebase otherwise (transition to `manual-resolution-required`). Verified by integration test 19c. |
| **R-M10** | Git LFS interference with markdown files | Step 11 - startup probe asserts no LFS filter rules apply to `*.md`; `engram init` runs `git lfs uninstall --local` |
| **R-M11** | Git version skew (Apple git vs Homebrew git) | Step 11 - startup probe asserts `git --version` >= 2.40 floor; logs `which git` |
| **R-M12** | `git pull` with uncommitted changes fails unpredictably | Step 8 - coordinator owns its own queue; never has uncommitted changes between cycles. If `engram serve` finds uncommitted changes at startup, **refuse to start** (FAIL doctor finding pointing the user to either commit, stash, or run `engram sync --resume` after manual review). Auto-committing arbitrary working-tree state under engram's name is a footgun (could include partial writes / editor swap files). |
| **R-M13** | Branch protection blocks force-push | Step 9 - state machine escalates to `manual-resolution-required` after N retries; never attempts `--force` against a protected branch |
| **R-M14** | Commit author identity leakage across machines | Step 11 - coordinator sets `user.email` / `user.name` per-vault via `git -c` flags, sourced from `.engram/identity.local`; never inherits from global |
| **R-M15** | Push rate-limiting / abuse detection from too-frequent pushes | Step 7 - hard floor 60s between push attempts regardless of capture volume; respect HTTP 429 / Retry-After if surfaced |

### Low severity

L1 (git binary missing) → Step 11 startup probe.
L2 (pack file growth) → `engram sync compact` documented in Step 16.
L3 (reflog growth) → Step 11 sets `gc.reflogExpire=30.days.ago` per-repo.
L4 (`safe.directory` warnings) → Step 11 detects and surfaces actionable doctor finding.
L5 (symlinked vault paths) → Step 11 resolves via `realpath`; warns if symlinked.
L6 (remote storage limits) → Step 13 doctor check verifies `.indexes/` and `*.sqlite*` are gitignored.
L7 (clock skew) → Documented; not blocking.
L8 (`.gitignore` drift) → Step 11 verifies `.gitignore` not locally edited before each cycle.

## Edge Cases

55 cases enumerated by the edge-case sub-agent; addressed by Plan steps or explicitly deferred. Categories:

* **Empty / null / zero (cases 1-9)** → Step 8 (empty-vault first-push, no-remote no-op, detached HEAD refusal, working-tree-dirty auto-commit, empty-commit skip).
* **Maximum sizes (cases 10-16)** → Step 7 (debounce coalesce for bulk migration), Step 17 (first-push-CLI separate from serve hook so blocking doesn't violate NFR1), Step 13 (gitignore assertion for `.indexes/`).
* **Concurrent access (cases 17-23)** → Step 9 (state machine), Step 12 (MigrationLock), Step 7 (asyncio.Lock around git invocations is owned by the coordinator), `engram sync` refuses if serve running (Step 15).
* **Error states / partial completion (cases 24-32)** → Step 9 (state machine recovery + retry policy), Step 13 (conflict marker doctor check), Step 11 (gpg / SSH probes).
* **Encoding / locale (cases 33-37)** → Step 11 (autocrlf probe), Step 19 (integration test with non-ASCII vault path), `engram init` warns if vault path has decomposable Unicode segments engram writes to.
* **Network failures (cases 38-44)** → Step 9 (retry classification: DNS transient up to 3 attempts, 401/404 permanent never-retry, 5xx exponential backoff), Step 2 (`sync.push_timeout_seconds` configurable, default 60s, distinct from `startup_pull_timeout_seconds=3.0`).
* **Special git states (cases 45-55)** → Step 8 (detached HEAD refusal), Step 11 (submodule, worktree, bare repo refusals), Step 11 (gpgsign + hooks probes), commit invocations pass `--no-verify` by default with `sync.use_no_verify` opt-out.

**Explicitly deferred to Phase 3+:**

* Submodules under `thoughts_dir` (Step 11 refuses; explicit non-feature).
* Sparse-checkout / partial-clone optimizations for very large vaults (>50K thoughts is out-of-scope NFR1 budget).
* Multi-remote setups (multiple `git remote add`) - Phase 2 supports exactly one configured remote per vault.

## Plan

The plan is layered (config + errors → utils → coordinator → server → CLI → doctor → tests → docs) and TDD-paired. Steps mostly follow Phase 1's "tooling first, tests alongside" cadence. Total: 21 ordered steps across 8 layers.

### Layer A - Config + errors (Steps 1-3)

**1. Add `SyncError` to `engram.errors`** -> verify: `from engram.errors import SyncError; SyncError("test").error_code == "SyncError"` succeeds.

**2. Extend `engram.config.models.SyncConfig`** with new Phase 2 fields, all `Field(...)`-typed and Pydantic-validated:
- `role: Literal["primary", "read-only"] = "primary"` (R-H3, edge 18)
- `disabled: bool = False` (kill-switch, edge cross-cutting)
- `debounce_window_seconds: float = Field(60.0, ge=1.0)` (R-M1, edge 13/14)
- `max_deferral_seconds: float = Field(300.0, ge=10.0)` (edge 14)
- `push_retry_count: int = Field(3, ge=0)` (R-M5/M6, edge 26/27)
- `push_retry_backoff_seconds: float = Field(1.0, ge=0.1)` (edge 26)
- `push_timeout_seconds: float = Field(60.0, ge=1.0)` (edge 41)
- `allow_unsigned: bool = False` (R-H8)
- `use_no_verify: bool = True` (edge 53)
- `signed_pull_required: bool = False` (R-H2; opt-in)
- `expected_remote_pattern: str | None = None` (R-H3; e.g. `r"^git@github.com:kpachhai/.*-personal\.git$"`)

Update `loader._coerce_sync_config()` to honor the new fields. Update `engram init` starter `engram.config.yaml` template with commented-out examples. -> verify: `tests/config/test_sync_config.py` round-trips all fields with expected defaults; rejects `role: "primary-or-readonly"` invalid value.

**3. Define new doctor check codes in `engram.diagnostics.doctor`** as plain string constants (no enum needed) so future checks reference them by name: `git_version_floor`, `branch_alignment`, `conflict_markers_present`, `cloud_sync_under_dotgit`, `gitignore_indexes`, `signed_commits_required`, `lfs_drift`, `autocrlf_drift`, `submodule_under_vault`, `gpg_agent_reachable`, `vault_identity_remote_match`, `sync_user_identity_set`, `working_tree_dirty_at_startup`, `read_only_role_contradicts_auto_push`. -> verify: `tests/diagnostics/test_doctor_codes.py` asserts all 14 names are stable strings.

### Layer B - gitops utility module (Steps 4-6)

**4. Create `src/engram/sync/__init__.py` + `src/engram/sync/gitops.py`** as a thin async layer over `run_git`. Functions, all `async def` and all returning typed results (no raw `CompletedProcess`):
- `is_inside_work_tree(cwd)` -> `bool`
- `current_branch(cwd)` -> `str | None` (None if detached)
- `remote_url(cwd, remote)` -> `str | None`
- `default_remote_branch(cwd, remote)` -> `str | None`
- `status_porcelain(cwd)` -> `list[StatusEntry]` typed dataclass
- `ahead_behind_count(cwd, branch, remote)` -> `tuple[int, int]`
- `commit_paths(cwd, paths, *, message, user_email, user_name, no_verify, allow_empty)` -> `CommitResult` (sha + message)
- `fetch(cwd, remote, *, timeout)` -> `FetchResult`
- `pull_rebase(cwd, remote, branch, *, timeout)` -> `PullResult` (with `conflicts: list[Path]` field)
- `push(cwd, remote, branch, *, force_with_lease, timeout)` -> `PushResult`
- `verify_commit(cwd, ref, allowed_keys)` -> `bool`
- `git_version(cwd)` -> `tuple[int, int, int]`

Each parses `run_git(...).stderr` for known patterns and classifies via a `GitErrorClass` enum (`AUTH`, `NETWORK_TRANSIENT`, `NETWORK_PERMANENT`, `NON_FAST_FORWARD`, `CONFLICT`, `LOCK_HELD`, `UNKNOWN`). -> verify: `tests/sync/test_gitops.py` mocks `run_git` for each branch and asserts correct classification per response. **PLUS `tests/sync/test_gitops_real_git_smoke.py`** runs at least one real `git` invocation per error class against a `tmp_path / "remote.git"` bare repo (e.g. push to a wrong-credentials URL for AUTH; push to a remote that's ahead for NON_FAST_FORWARD; rebase against divergent history for CONFLICT). This locks the parser against drift between mock fixtures and real git stderr. **TDD checkpoint:** write tests first.

**5. Implement `conflict_marker_scan(thoughts_dir)` in `engram.sync.gitops`** that walks markdown files and returns `list[Path]` of files with `<<<<<<< ` / `=======` / `>>>>>>> ` markers. **Whole-file scan** - markers can appear anywhere git wrote a hunk, and security wins over the 8KB optimization at engram's typical thought-size budget (<2KB). -> verify: `tests/sync/test_conflict_marker_scan.py` covers (a) empty dir, (b) clean dir, (c) marker in body, (d) marker only in frontmatter, (e) marker beyond 8KB into the file (proves no early-exit), (f) marker only in `=======` (the hunk separator alone, without `<<<<<<<` / `>>>>>>>`, must NOT trigger a false positive).

**6. Implement `IdentityCheck` in `engram.sync.identity`** reading `.engram/identity.local` (gitignored, machine-local). Schema: `vault_id: str` + `expected_remote_pattern: regex`. Returns `Match` / `Mismatch(actual_url, expected_pattern)` / `MissingIdentity`. -> verify: `tests/sync/test_identity.py` covers match, mismatch (cross-vault contamination simulated), missing identity file.

### Layer C - Sync coordinator state machine (Steps 7-10)

**7. Define `SyncCoordinator` state machine in `src/engram/sync/coordinator.py`** with explicit states as `enum.StrEnum`:
- `idle`, `debouncing`, `committing`, `committed_not_pushed`, `fetching`, `pushing`, `paused-for-migration`, `auth-required`, `manual-resolution-required`, `disabled` (10 states total; `committed_not_pushed` is the durable persisted-locally-but-not-replicated state for R-H5 resume-on-startup)

Each state has explicit allowed transitions (validated at runtime, raising `SyncError` on disallowed). The coordinator owns:
- An `asyncio.Queue[Path]` for capture-queued files
- A debounce timer (resets on each enqueue; fires after `debounce_window_seconds` of idle)
- A max-deferral timer (forces commit after `max_deferral_seconds` of continuous activity)
- A single `asyncio.Lock` around the actual git invocation (edge 22)
- A structured `list[SyncEvent]` ring buffer (last 256 events) for doctor inspection

-> verify: `tests/sync/test_coordinator_state_machine.py` asserts every documented transition succeeds and every undocumented transition raises `SyncError`. Plus a hypothesis property test: for any sequence of `(capture, fetch_result, push_result)` events from the modeled set, the coordinator never enters an undocumented state and always reaches `idle` OR a terminal-error state (`auth-required` / `manual-resolution-required`) within bounded steps. **TDD: write the state-transition table and tests before the implementation.**

**8. Implement coordinator commit path** (idle → debouncing → committing → idle):
- On enqueue, reset debounce timer; if `debouncing` already, just append; if `idle`, transition to `debouncing`.
- On debounce-fire OR max-deferral-fire, transition to `committing`.
- Read all queued paths, call `gitops.commit_paths(..., message="engram: capture batch (N=K)", ...)` with `--no-verify` (per `sync.use_no_verify`) and per-vault `user.email`/`user.name`.
- Skip if `git status --porcelain` shows nothing staged (edge 8).
- On detached HEAD (edge 5/45), refuse: transition to `manual-resolution-required`.
- On working-tree-dirty at startup with non-engram files, auto-commit with message `engram: external edit reconcile` (edge 7).

-> verify: `tests/sync/test_coordinator_commit.py` covers debounce coalesce, max-deferral, detached-HEAD refusal, external-edit reconcile, empty-commit skip.

**9. Implement coordinator push path with retry classification** (committing → pushing → idle | committed_not_pushed | auth-required | manual-resolution-required):
- (9a) After successful commit, transition to `pushing`.
- (9a) Call `gitops.push(..., force_with_lease=False, timeout=push_timeout_seconds)`.
- (9a) On `NETWORK_TRANSIENT`: retry up to `push_retry_count` with exponential backoff.
- (9a) On `AUTH`: never retry; transition to `auth-required`.
- (9a) On `NETWORK_PERMANENT`: never retry; transition to `committed_not_pushed`.
- (9b) On `NON_FAST_FORWARD` - **explicit reflog gate before rebase** (R-M9): capture the previous `origin/<branch>` SHA via `git rev-parse origin/<branch>` BEFORE the fetch; run `gitops.fetch`; after fetch, assert previous SHA is reachable from new `origin/<branch>` via `git merge-base --is-ancestor <prev_sha> origin/<branch>`. If unreachable (force-push happened upstream, history rewritten), transition to `manual-resolution-required` and do NOT auto-rebase. If reachable, run `gitops.pull_rebase`. On clean rebase, retry push once with `force_with_lease=True`. On rebase conflict, transition to `manual-resolution-required`.
- (9c) On startup, run `ahead_behind_count`; if local ahead AND last state was `committed_not_pushed`, attempt resume-push (no commit; reuses 9a path).

-> verify: `tests/sync/test_coordinator_push.py` covers each error class with mock gitops, asserts state transitions and retry counts. Plus `tests/sync/test_coordinator_push_reflog_gate.py` simulating force-push upstream (manually rewrite remote ref via `git update-ref` in the bare repo); asserts the gate transitions to `manual-resolution-required` rather than auto-rebasing across the gap.

**10. Wire `_post_capture_sync()` to coordinator and add startup pull**: `VaultStorage._post_capture_sync(thought)` calls `self._sync_coordinator.enqueue(thought.file_path)` if the coordinator is set (set by `engram serve`, None otherwise so unit tests don't sync). Add `engram serve` startup hook: if `sync.auto_pull_on_startup`, run `gitops.pull_rebase` with `startup_pull_timeout_seconds`. -> verify: `tests/sync/test_storage_facade_wiring.py` confirms the call site fires; `tests/sync/test_serve_startup_pull.py` asserts pull is attempted with correct timeout and graceful no-op if no remote.

### Layer D - Identity + safety probes (Steps 11-12)

**11. Implement `engram.sync.startup_probes`** as a single async function `run_startup_probes(config, vault_dir)` returning `ProbeReport(failures: list[ProbeFailure], warnings: list[ProbeWarning])`. Each probe maps 1:1 to a doctor check code from Step 3 (so the same logic surfaces both at startup AND under `engram doctor`). Probes:

1. `git --version` >= 2.40 floor (R-M11, L1) → `git_version_floor`
2. `core.autocrlf == false` AND `.gitattributes` pins `*.md text eol=lf` (R-M3) → `autocrlf_drift`
3. No LFS filter rules apply to `*.md` (R-M10) → `lfs_drift`
4. `git rev-parse --is-inside-work-tree == true` (edge 49) - included in `branch_alignment` semantics
5. No submodules under `thoughts_dir` (edge 47) → `submodule_under_vault`
6. No worktree split (edge 48) - included in `branch_alignment` semantics
7. `git symbolic-ref refs/remotes/origin/HEAD` matches `sync.git_branch` (R-M2) → `branch_alignment`
8. `.indexes/` AND `*.sqlite*` AND `*.sqlite-wal` AND `*.sqlite-shm` are in `.gitignore` (L6, edge 15) → `gitignore_indexes`
9. `.git/` is NOT under a known cloud-sync root (R-H7) → `cloud_sync_under_dotgit`
10. If `commit.gpgsign=true` (global or local), gpg-agent is reachable (R-H8) → `gpg_agent_reachable`
11. `vault.identity` from `.engram/identity.local` matches `git remote get-url origin` against `expected_remote_pattern` (R-H3) → `vault_identity_remote_match`
12. `git config user.email` and `user.name` are set per-vault (or `.engram/identity.local` provides them) (R-M14) → `sync_user_identity_set`
13. Working tree is clean (no uncommitted changes from external editors) (R-M12, edge 7) → `working_tree_dirty_at_startup` - FAIL with actionable message: commit / stash / `engram sync --resume`
14. Config does NOT contradict itself: `role: read-only` AND `auto_push_on_capture: true` simultaneously is a misconfig (R-H3 defense-in-depth) → `read_only_role_contradicts_auto_push` - FAIL, do NOT silently override

`engram serve` calls `run_startup_probes` BEFORE acquiring `VaultLock`; on any FAIL, exits 2 with the failure list. WARNs are logged but don't block. Per-cycle re-checks of probes 7 (branch_alignment) and 11 (vault_identity_remote_match) run before every push so mid-session admin changes (re-pointed `origin/HEAD`, swapped remote URL) don't leak; per-cycle is a separate cheap check, not a re-run of all 14 probes.

-> verify: `tests/sync/test_startup_probes.py` covers each probe (positive + negative).

**12. Add `MigrationLock` to `engram.utils.lock`** as a separate flock at `<vault>/.indexes/migration.lock`. `engram migrate-from-open-brain` acquires it for the duration of migration. Sync coordinator checks for it before every git invocation; if held, transitions to `paused-for-migration`. On migration end, lock released; coordinator's next enqueue resumes normal flow. -> verify: `tests/sync/test_migration_lock_pause.py` runs concurrent migrate + sync; asserts no commits during migration window.

### Layer E - Doctor extensions (Step 13)

**13. Add fourteen `_check_*` functions to `engram.diagnostics.doctor`** matching the codes from Step 3 (most reuse the probe logic from Step 11 - the doctor and startup-probe path share an internal helper module):

- `_check_git_version_floor` - WARN if <2.40, FAIL if missing
- `_check_branch_alignment` - WARN if local branch != `sync.git_branch`, FAIL if detached HEAD or worktree split
- `_check_conflict_markers_present` - FAIL if `gitops.conflict_marker_scan(thoughts_dir)` non-empty
- `_check_cloud_sync_under_dotgit` - FAIL if `.git/` resolves under iCloud / Dropbox / OneDrive / Google Drive
- `_check_gitignore_indexes` - FAIL if `.indexes/` (or `*.sqlite*`) not in `.gitignore`
- `_check_signed_commits_required` - WARN if `signed_pull_required=true` but `~/.config/engram/trusted-keys.yaml` missing or empty
- `_check_lfs_drift` - WARN if any `.gitattributes` LFS filter applies to `*.md`
- `_check_autocrlf_drift` - FAIL if `core.autocrlf=true` AND `.gitattributes` doesn't pin `eol=lf`
- `_check_submodule_under_vault` - FAIL if any submodule path under `thoughts_dir`
- `_check_gpg_agent_reachable` - WARN if `commit.gpgsign=true` AND agent unreachable AND `allow_unsigned=false`
- `_check_vault_identity_remote_match` - FAIL on cross-vault contamination per `IdentityCheck`
- `_check_sync_user_identity_set` - WARN if `user.email` / `user.name` not set per-vault (R-M14)
- `_check_working_tree_dirty_at_startup` - WARN at runtime via doctor (FAILed at startup; runtime is informational since serve is already past the gate)
- `_check_read_only_role_contradicts_auto_push` - FAIL if `role: read-only` AND `auto_push_on_capture: true` (config contradiction)

Each adds `report.add(name, status, message, detail)`; doctor's `exit_code` already maps to 0/1/2. -> verify: `tests/diagnostics/test_sync_checks.py` covers all 14 with positive + negative cases.

### Layer F - CLI (Steps 14-17)

**14. Implement `engram clone-vault <url> <local_path>` subcommand** in `src/engram/cli/clone.py`. Performs: (a) `git clone --no-checkout <url> <local_path>`, (b) `rm -rf <local_path>/.git/hooks`, (c) `(cd <local_path> && git checkout)`. Writes `.engram/identity.local` template. Refuses if `<local_path>` exists and is non-empty. -> verify: `tests/cli/test_clone.py` runs against a real `git init --bare` source repo whose `.git/hooks/post-checkout` contains a hook that touches a sentinel file (`/tmp/engram-test-hook-fired-<uuid>`). After `engram clone-vault` runs, assert the sentinel file does NOT exist - this proves the security property (R-H1: hooks deleted BEFORE checkout phase fires them), not just the implementation order.

**15. Implement `engram sync` subcommand** in `src/engram/cli/sync.py` with flags `--push`, `--pull`, `--first-push`, `--resume`. Behaviour:
- Refuses if `engram serve` is running (`VaultLock` held by another PID with `process.cmdline()` matching `engram serve`); points user at "stop server first OR `engram serve` already syncs automatically."
- `--first-push`: empty-repo bootstrap (initial commit + `git push -u origin <branch>`), used after `engram init` + adding remote.
- `--pull`: explicit pull; useful for work machine where `auto_pull_on_startup=false`.
- `--push`: explicit push of any committed-not-pushed state.
- `--resume`: probe `ahead_behind_count` and run the appropriate recovery (commit pending changes if any + push).
- Default (no flags): pull then push.

-> verify: `tests/cli/test_sync.py` covers each flag against a tmp `git init --bare` remote.

**16. Implement `engram sync compact` subcommand** in `src/engram/cli/sync.py` (or split file). Runs `git gc --auto` + sets `gc.reflogExpire=30.days.ago` per-repo (L2, L3). Documented as quarterly maintenance. -> verify: `tests/cli/test_sync_compact.py` asserts the gc invocation; verifies reflogExpire is configured.

**17. Wire `engram serve` startup ordering**:
1. Run `run_startup_probes` (Step 11). On FAIL, exit 2.
2. Acquire `VaultLock`.
3. If `sync.auto_pull_on_startup`, run startup pull (Step 10).
4. Scan markdown for conflict markers (Step 5). If found, enter degraded mode (search OK, capture refused).
5. Build `SyncCoordinator`, attach to `VaultStorage`.
6. Build FastMCP server.
7. On shutdown: drain coordinator queue (commit + push pending) before releasing lock.

-> verify: `tests/cli/test_serve_startup_order.py` patches each layer and asserts call ordering. Drain-on-shutdown verified by `tests/sync/test_drain_on_shutdown.py`.

### Layer G - Integration tests (Steps 18-19)

**18. Build `tests/sync/conftest.py` integration harness**: `bare_remote` fixture creates `git init --bare` in `tmp_path / "remote.git"`; `vault_a` and `vault_b` fixtures clone the bare repo into separate `VaultStorage` instances with distinct `.engram/identity.local` files. Helpers: `capture_thought_in(vault, content)`, `sync_now(vault)`, `merge_marker_into(vault, thought)` (test fixture for R-H6). -> verify: harness self-test confirms two-clone setup is reachable from both.

**19. Build sweep of integration tests** at `tests/sync/test_two_machine_convergence.py` covering:

a. `test_two_machine_convergence_happy_path`: capture on A, sync, fetch+pull on B, B sees the thought.
b. `test_concurrent_capture_no_conflict`: A captures X, B captures Y simultaneously; both push (B retries after fetch+rebase); both end up in remote.
c. `test_force_push_elsewhere_triggers_degraded_mode`: external force-push wipes a local commit on B; B's next pull refuses to auto-rebase; B exits 2 with manual-resolution message (R-M9).
d. `test_pull_with_conflict_markers`: simulate a conflicted pull; coordinator scans, finds markers, enters degraded mode; capture refused; search OK (R-H6).
e. `test_first_push_empty_vault`: empty vault, `engram sync --first-push`; remote receives initial commit + branch upstream set (edge 1).
f. `test_no_remote_no_op`: `git remote` not configured; capture succeeds, doctor reports "no remote; commits land locally only," no errors logged (edge 4).
g. `test_read_only_role_contradicts_auto_push_refuses_start`: vault config `sync.role: read-only` AND `auto_push_on_capture: true` is a config contradiction; `engram serve` startup probe (Step 11 probe 14) FAILs with `read_only_role_contradicts_auto_push`; serve exits 2; user must reconcile config. (R-H3 defense-in-depth: never silently override - misconfig must surface, not hide.)
h. `test_read_only_role_refuses_push`: vault config `sync.role: read-only` AND `auto_push_on_capture: false` (consistent config); coordinator never enters `pushing` state; CLI `engram sync --push` returns code `vault_read_only`.
i. `test_migration_pauses_sync`: start `migrate-from-open-brain` against a stub OB. Use an explicit `threading.Event` barrier the migration sets when it has acquired `MigrationLock` and is mid-loop; the test only fires the sync attempt after the barrier so the interleave is deterministic (not race-based). Assert sync coordinator transitions to `paused-for-migration` and no commits land during migration window (R-H9).
j. `test_unicode_vault_path`: vault path includes `Документы`; full capture+sync round-trip succeeds (edge 33).
k. `test_drain_on_shutdown`: enqueue 5 captures, send SIGTERM; assert all 5 are committed before lock releases.

Each test is hermetic (own `tmp_path`); none reach the network. -> verify: full sweep passes locally; same suite added to `.github/workflows/ci.yml` test matrix.

### Layer H - Docs (Steps 20-21)

**20. Author ADR 005 - "Sync coordinator state machine"** at `docs/adr/005-sync-coordinator.md`. Status, context, decision, consequences, alternatives. Names the explicit states + transition table; documents why `--force-with-lease` not `--force`; documents read-only role enforcement; documents the cross-vault contamination check. -> verify: `wc -l docs/adr/005-sync-coordinator.md` is between 80 and 200 lines (similar to ADRs 001-004).

**21. Update `docs/PHASE_2_CODE_COMPLETE.md` validation doc + per-machine vault isolation doc + README + CHANGELOG**:
- `docs/PHASE_2_CODE_COMPLETE.md`: parallel of `PHASE_1_CODE_COMPLETE.md`; lists Phase 2 deliverables 1-7 from ROADMAP; splits code-side criteria from operational criteria.
- New `docs/MULTI_MACHINE_SETUP.md`: separate-repo model per `02-TECHNICAL_DESIGN.md` Vault Isolation; step-by-step for personal + work machine; `.engram/identity.local` template; `engram clone-vault` flow.
- README "Status" section: Phase 2 added.
- CHANGELOG `[Unreleased]`: every Phase 2 commit grouped under Added / Changed / Security.

-> verify: each doc readable; cross-references resolve; `engram doctor` invocation in MULTI_MACHINE_SETUP.md actually works against a clean install.

## Open Questions

These need user input before execution. Each is followed by a recommended default the implementation will use unless the user redirects.

**Q1**: Should `engram clone-vault` be Phase 2 (Step 14) or Phase 3? It's the safe-clone flow (R-H1) but is also adjacent to multi-vault management which is Phase 3.
- **Default**: Phase 2 - hooks-deletion is a security mitigation, not a multi-vault feature. The command stays minimal (clone + delete hooks + checkout); multi-vault aliasing is Phase 3.

**Q2**: Should `signed_pull_required` (R-H2) default to `true` or `false`?
- **Default**: `false`. Most users won't have signed-commit infrastructure on day one; making it required at the start would block adoption. Document it as the recommended hardening; ship with the doctor check WARNing.

**Q3**: Should the coordinator's debounce floor be the same as the push rate-limit floor (60s)?
- **Default**: yes - the simplest model is "one commit per debounce window, one push per commit," and the human user's commit-pace tolerance is roughly the same as a remote's rate-limit tolerance (both want sparse, batched commits). A future advanced setting can split them if a real workload demands.

**Q4**: How should `engram sync --first-push` interact with a non-empty `thoughts/` dir that was migrated from Open Brain BEFORE the remote was added? Specifically: does the first commit message need to mention "post-migration" so the user can find it in `git log`?
- **Default**: yes; if `git log -1` shows the OB migration commit, the first-push commit message is `engram: pre-sync baseline (post-migration)`.

**Q5**: ADR 005 - should it cover only the state machine, or also the cross-vault contamination check (R-H3)?
- **Default**: cover both in ADR 005; the contamination check is a load-bearing decision and deserves the rationale capture.

**Q6**: Should `engram serve` refuse to start if `_check_signed_commits_required` WARNs (i.e., `signed_pull_required=true` but no trusted keys file), or just log loudly?
- **Default**: refuse to start. `signed_pull_required=true` is an opt-in; if the user opted in, they meant it. Misconfiguration is a worse failure than refusing to start.

**Q7**: Should `auto_commit_on_capture` default change from `true` to `false`? With debounce + max-deferral floor of 60s, the commits are batched anyway; but disabling by default forces explicit `engram sync` invocations and may be safer for new users.
- **Default**: keep `true`. The explicit-only model defeats the multi-machine convergence goal; the debounce floor already addresses the "thousands of commits" risk (R-M1).

## Critique Pass

After draft synthesis, the 4th sub-agent (`code-reviewer`) was dispatched against this plan. Findings (1 Blocking, 13 Should-Fix, 4 Nice-to-Have).

**Blocking (incorporated):**
- (b-1) **R-M9 reflog check missing from Step 9** — Exit Criterion 5 (force-push-elsewhere defense) depended on a reflog gate that wasn't in the implementation steps. Step 9 now has explicit sub-step 9b: capture previous `origin/<branch>` SHA before fetch; assert reachability via `git merge-base --is-ancestor` after fetch; refuse to auto-rebase if unreachable.

**Should-Fix (all incorporated):**
- (sf-1) Step 7 enum was missing `committed_not_pushed` despite Step 9 referencing it — added; total now 10 states.
- (sf-2) R-H4 mitigation conflated new-thought races (mitigated) with same-thought concurrent edits (documented limitation under M-8) — risk table clarifies the distinction.
- (sf-3) R-H2 mitigation table now explicitly says "off by default" so the as-shipped config is honest about the gap.
- (sf-4) R-M12 auto-commit on dirty working tree was a footgun — changed to **refuse to start**, surfaced via new doctor check `working_tree_dirty_at_startup`.
- (sf-5) Step 14 (`clone-vault`) test was mocked subprocess — changed to a real `git init --bare` source with a malicious `post-checkout` hook + sentinel file assertion, proving the security property not just the call order.
- (sf-6) Step 19g (`test_read_only_role_refuses_push`) silently overrode contradictory config — split into 19g (contradiction FAILs startup probe) and 19h (consistent read-only config behaves correctly). New doctor check `read_only_role_contradicts_auto_push`.
- (sf-7) Step 11 had 12 probes against 11 doctor codes — added 3 new codes (`sync_user_identity_set`, `working_tree_dirty_at_startup`, `read_only_role_contradicts_auto_push`); now 14 probes ↔ 14 codes.
- (sf-8) Step 5 `conflict_marker_scan` had a contradicting 8KB scope vs whole-file claim — pinned to whole-file with a false-positive test for `=======` alone.
- (sf-9) Step 4 (`gitops` parser) was mock-only — added `test_gitops_real_git_smoke.py` against a real bare repo per error class, locking the parser against drift between mock fixtures and real git stderr.
- (sf-10) Step 7 state-machine test was tautological (verifies the table matches itself) — added a hypothesis property test asserting bounded reachability of `idle` or terminal-error states for any modeled event sequence.
- (sf-11) Step 12 (`test_migration_lock_pause`) was race-based — added explicit `threading.Event` barrier so the interleave is deterministic.
- (sf-12) Cross-references "Step 5" / "Step 10" for asyncio.Lock + push_timeout were wrong — corrected to Step 7 + Step 2.
- (sf-13) R-M2 + probe 7 once-at-startup gap — clarified that probes 7 + 11 also re-run before every push, so mid-session admin changes don't leak.

**Nice-to-Have (deferred with reason):**
- (nh-1) Step 14 `engram clone-vault` is partly Phase 3 territory — kept in Phase 2 because the hooks-deletion is a security mitigation, not a multi-vault feature; the command stays minimal.
- (nh-2) Step 15 `--first-push` and `--resume` flags are sugar — kept because they make the operator workflow self-documenting; cost is small.
- (nh-3) Step 16 `engram sync compact` could ship as `engram init`-time config rather than a CLI command — kept because L2/L3 maintenance is a real ongoing concern at multi-year scale; the CLI surface is the natural home.
- (nh-4) The structured event log in Step 7 could expose via MCP for in-process introspection — deferred; doctor + log file is enough for Phase 2.

## Sub-Agent Findings Summary

* **Code analysis** read 11 files. Confirmed all Phase 2 plug-in points exist (`_post_capture_sync` stub, `run_git`, `VaultLock`, `SyncConfig` with 6 fields, `register(app)` CLI pattern, `CheckResult` doctor framework).
* **Risk** flagged 31 prioritized failure modes (10 High, 15 Medium, 6 Low). Top three by impact: cross-vault contamination (R-H3), markdown injection via attacker remote (R-H2), conflict markers corrupting Pydantic parse (R-H6). All addressed by Plan steps.
* **Edge cases** flagged 55 boundary conditions across 7 categories. The empty-vault first-push (1), bulk-migration coalesce (13/14), and conflict-marker scan (25) are the three load-bearing ones. All addressed by Plan steps.
* **Critique** surfaced 3 Should-Fix findings (incorporated) and 3 Nice-to-Have (deferred). No Blocking findings.

## Implementation Notes

* Steps 1-3 are independent; can be done in parallel.
* Steps 4-6 depend on 1-3.
* Steps 7-10 depend on 4-6 (gitops + identity + state-machine atomicity must exist first).
* Steps 11-12 depend on 4-6.
* Step 13 depends on 5 (conflict marker scan), 6 (identity), 11 (probes).
* Steps 14-17 depend on 7-13.
* Step 18 depends on 14-17.
* Step 19 depends on 18.
* Steps 20-21 are last and depend on the rest.

A reasonable single-session checkpoint cadence: commit-and-push after each layer (A, B, C, D, E, F, G, H = 8 checkpoints). Per the dotfiles `Wrap-and-clear` rule, a session-wrap fires after each layer.

**Estimated effort**: 2-3 weeks of focused work, mirroring Phase 1's pace. Step 19 (integration tests) is the longest single step; Step 11 (startup probes) has the most external surface area; Step 9 (push state machine) has the most internal subtlety.

## Phase 2 Exit Criteria (Per ROADMAP)

Phase 2 is shipped and stable when ALL true:

1. Two physically-separate clones converge on captured thoughts within one debounce window (Step 19a).
2. Read-only role enforcement prevents work machine from pushing (Step 19g).
3. Cross-vault contamination check refuses misconfigured remotes (R-H3, Step 11 probe 11).
4. Conflict marker scan + degraded mode work end-to-end (Step 19d).
5. Force-push elsewhere does not silently lose local commits (Step 19c).
6. Migration pauses sync (Step 19h).
7. `engram sync` and `engram clone-vault` CLI commands work as documented.
8. All 11 new doctor checks have known-good and known-bad test cases.
9. CI matrix passes (Python 3.11 + 3.12, macOS + Ubuntu).
10. ADR 005 published; MULTI_MACHINE_SETUP.md published.
11. Author runs Phase 2 across two of their own machines for at least 7 consecutive days without falling back to manual git commands.
