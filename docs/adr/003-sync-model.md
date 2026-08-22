# ADR 003 - Sync model: system git CLI, no library

## Status

Accepted (Phase 1; sync coordinator lands in Phase 2+).

## Context

engram is a multi-machine personal-memory backend. The same vault (markdown + index) needs to converge across a personal laptop, a work laptop, an iPad-via-SSH, etc. The sync transport is the single biggest design risk - get it wrong and the user loses thoughts or, worse, silently overwrites them.

Three real-world constraints shape the choice:

1. **Conflicts are inevitable.** Two machines edit the same thought between syncs. The transport has to either prevent conflicts (server-side locking - rejected by NFR3) or expose them as first-class events.
2. **Users already trust git.** Every developer-adjacent user already has a git remote (GitHub, GitLab, self-hosted Gitea). Adding a custom replication protocol would mean asking them to trust a new service.
3. **Cross-machine privacy boundaries are real.** A user's personal vault must not be reachable from their work laptop. Two physically separate git remotes is the simplest possible enforcement.

## Decision

The sync transport is the **system `git` CLI**, invoked via `subprocess.run([...])` with a curated environment. No `pygit2`, no `dulwich`, no embedded library.

* **Why system git, not a library?** Battle-tested on every platform we care about. No version-skew bugs between the engram bundle and the user's git installation. Transparent to the user - they can `git log` their own vault. Conflict resolution is delegated to git's well-understood model (merge, rebase, or external tool).
* **Why a curated environment?** Four env vars are pre-staged on every git invocation per `02-TECHNICAL_DESIGN.md` Flow C: `GIT_TERMINAL_PROMPT=0` (never prompt for credentials), `GIT_MERGE_AUTOEDIT=no` (never open the editor), `GIT_ASKPASS=true` (no interactive password fallback), `GIT_LFS_SKIP_SMUDGE=1` (don't fetch LFS on the hot path). The wrapper helper lives at `src/engram/utils/run_command.py` (`run_git`).
* **Why not in Phase 1?** Phase 1 is single-machine. Flow A captures leave a `_post_capture_sync()` stub on `VaultStorage`. The Phase 2 sync coordinator wires that hook to a debounced background `git add/commit/push` per `02-TECHNICAL_DESIGN.md` Flow D.

## Consequences

### Positive

* Zero new infrastructure for the user to manage.
* Conflict semantics are git's, not ours. We don't have to invent CRDTs, last-write-wins, or vector clocks.
* Privacy boundary is enforced by physically separate remotes - the work vault repo simply doesn't have the personal remote configured.
* `engram doctor` can shell out to `git status` for sync health checks.

### Negative

* Sync latency is bounded by git push (typically 1-3s for a small commit on a stable connection). Acceptable for personal scale; not acceptable for a real-time-collaboration use case (which is explicitly out of scope through Phase 5).
* Conflict UX is git's: a merge conflict in a markdown file lands as `<<<<<<< HEAD` markers. Mitigation in Phase 2: `engram doctor` detects conflict markers and refuses to start the server until they are resolved.
* git over SSH requires the user to manage SSH keys. Mitigation: same set the user already manages for their normal dev work; engram does not add a new key surface.

## Alternatives considered

* **Custom replication protocol over an engram-controlled service** - rejected. Violates NFR3 (privacy/sovereignty); creates a hosted dependency; fundamentally re-creates the Open Brain failure mode this project exists to solve.
* **Dropbox / iCloud / Google Drive as the transport** - rejected as the *primary* mechanism. Cloud-sync paths are detected at startup (Q10 default: WARN) because they break atomic writes (the cloud sync agent can pick up `<file>.tmp` mid-rename). Q10 leaves them allowed-with-warning rather than hard-blocked because some users genuinely accept the risk for read-only mirroring.
* **Filesystem replication (Syncthing, rsync)** - viable for advanced users; documented as a fallback but not the primary path. Same atomic-write concern as cloud sync.
* **Embedded git library (`pygit2`, `dulwich`)** - rejected. Adds a large dependency for no functional gain over the system git the user already trusts.

## References

* Original spec: Flow C and Flow D (technical design) plus the multi-machine sync
  milestone (roadmap), in the maintainer's unpublished planning repo.
* `src/engram/utils/run_command.py` - `run_git` wrapper with curated env
