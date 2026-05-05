# Multi-machine setup

This is the operator-facing guide for configuring engram to keep two
or more machines converged on the same set of thoughts. It is the
companion document to ADR 005 (sync coordinator state machine).

## When to use this

- You want your captured thoughts on a personal laptop AND a desktop.
- You want a work laptop to READ from a separate vault but never
  push (so personal context never leaks into work).
- You want crash safety: if one machine dies, the other is current.

## When NOT to use this

- You only use one machine. Phase 1 is enough; skip this guide.
- You want shared editing across MANY users. Phase 2 is single-author
  multi-machine; team / org editing is Phase 4+.
- Your `.git/` directory lives under a consumer cloud-sync provider
  (Dropbox, iCloud, Google Drive, OneDrive). engram FAILs at startup
  on those - SQLite + cloud-sync semantics are unreliable. Use a
  non-synced directory and rely on git transport instead.

## Architecture

Each machine has its OWN clone of the same git remote. There is NO
shared filesystem - each machine is independent, and convergence happens
via push/pull through the remote.

```
                          +-----------------+
                          |  git remote     |
                          |  (bare repo)    |
                          +--------^--------+
                                   |
                  +----------------+----------------+
                  |                                 |
        +---------+--------+              +---------+--------+
        |  personal laptop |              |   desktop        |
        |  vault path:     |              |   vault path:    |
        |  ~/engram/       |              |   ~/engram/      |
        |  role: primary   |              |   role: primary  |
        +------------------+              +------------------+
```

Each clone has:
- Its own `.indexes/engram.db` (SQLite with sqlite-vec)
- Its own `.engram/identity.local` (machine-local; NOT committed)
- Its own `engram.config.yaml`

The git remote has only the `thoughts/` markdown files. The SQLite
index is gitignored and rebuilt locally on each machine via reindex.

## Step-by-step

### 1. Create the bare remote

Create a bare git repo somewhere your machines can reach (GitHub
private repo, self-hosted Gitea, etc.):

```bash
ssh user@host 'cd ~ && mkdir engram-personal.git && cd engram-personal.git && git init --bare'
```

### 2. Bootstrap on the first machine

```bash
engram init ~/engram-personal
cd ~/engram-personal
git remote add origin git@host:user/engram-personal.git

# Add the safety entries to .gitignore (engram doctor catches missing entries).
cat > .gitignore <<EOF
.indexes/
*.sqlite
*.sqlite-wal
*.sqlite-shm
EOF

# Pin line endings + drop git LFS for *.md.
cat > .gitattributes <<EOF
*.md text eol=lf
EOF

# Tag the vault identity.
mkdir -p .engram
cat > .engram/identity.local <<EOF
vault_id: example-personal
expected_remote_pattern: '^git@host:user/engram-personal\.git$'
user_email: you@example.com
user_name: Your Name
EOF

# First push.
engram sync --first-push --config $(pwd)/engram.config.yaml
```

### 3. Clone onto the second machine

Use `engram clone-vault` rather than plain `git clone` - it removes
`.git/hooks/` BEFORE checkout so a malicious post-checkout hook in the
remote cannot execute (R-H1):

```bash
engram clone-vault git@host:user/engram-personal.git ~/engram-personal
```

Then reproduce the per-machine identity:

```bash
cd ~/engram-personal
cat > .engram/identity.local <<EOF
vault_id: example-personal
expected_remote_pattern: '^git@host:user/engram-personal\.git$'
user_email: you@example.com
user_name: Your Name
EOF

# Per-vault git identity (so this machine's commits are tagged with the
# vault user, not your global git config).
git config user.email you@example.com
git config user.name 'Your Name'
git config commit.gpgsign false   # or true if you have GPG infra
```

### 4. Verify the setup

```bash
engram doctor --config ~/engram-personal/engram.config.yaml
```

This runs all 9 Phase 1 checks plus the 14 Phase 2 sync checks. Every
row should be OK or WARN; FAILs must be resolved before serving.

### 5. Start the server

```bash
engram serve --config ~/engram-personal/engram.config.yaml
```

The startup ordering is:

1. Run all 14 startup probes; on any FAIL, exit 2.
2. Acquire the per-vault advisory lock (`flock`).
3. If `sync.auto_pull_on_startup=true` (default), run a startup pull.
4. Scan `thoughts/*.md` for conflict markers; if found, refuse to serve.
5. Build the sync coordinator + attach to storage.
6. Start FastMCP stdio loop.
7. On shutdown: drain pending commits + push, then release lock.

## Read-only work-machine pattern

A common pattern: a work laptop wants to READ personal thoughts but
must never push. Use a separate vault on the work machine with a
distinct remote AND `sync.role=read-only`:

```yaml
# work-machine: ~/engram-work/engram.config.yaml
sync:
  git_remote: origin
  git_branch: main
  role: read-only          # never push
  auto_pull_on_startup: true
  auto_commit_on_capture: false  # captures stay local-only
  auto_push_on_capture: false
```

The probe `read_only_role_contradicts_auto_push` FAILs if you ever set
`role: read-only AND auto_push_on_capture: true` simultaneously - that
combination is a config contradiction and engram refuses to start.

## Day-to-day operations

### Add a thought

Just run engram serve and use Claude Code (or any MCP client) to call
`capture_thought`. The coordinator debounces commits over the
configured `debounce_window_seconds` (default 60s) so rapid captures
batch into one commit.

### Manual sync (when serve is not running)

```bash
engram sync                      # pull then push
engram sync --pull               # explicit pull only
engram sync --push               # explicit push only
engram sync --resume             # commit any pending dirty state + push
```

`engram sync` refuses to run while a serve loop holds the vault lock;
either stop the serve loop or rely on its automatic sync.

### Quarterly maintenance

```bash
engram sync compact
```

Runs `git gc --auto` and pins `gc.reflogExpire=30.days.ago` so the
reflog does not grow without bound (L3 mitigation).

## Recovery scenarios

### "engram refused to start"

Run `engram doctor --config ~/engram-personal/engram.config.yaml` and
read the FAIL rows. The most common causes:

- `working_tree_dirty_at_startup` - commit, stash, or
  `engram sync --resume`.
- `gitignore_indexes` - your `.gitignore` is missing `.indexes/` and
  `*.sqlite*`.
- `vault_identity_remote_match` - your `expected_remote_pattern` does
  not match `git remote get-url origin`. Either fix the pattern or fix
  the remote URL.
- `cloud_sync_under_dotgit` - move the vault out of Dropbox / iCloud /
  Google Drive / OneDrive.
- `read_only_role_contradicts_auto_push` - your config has both flags
  on; pick one.

### "I see committed_not_pushed in the doctor output"

A network blip caused a push to fail. Run `engram sync --resume` to
retry the push. If it keeps failing, check your network and your auth
configuration (SSH key loaded? token expired?).

### "I see manual_resolution_required"

A force-push happened upstream and the reflog gate refused to silently
auto-rebase. Inspect `git log --oneline --all` and decide whether to
keep your local commits (rebase manually) or accept the rewritten
history (`git reset --hard origin/main`). Both are operator decisions;
engram refuses to make them silently.

### "I see conflict markers"

A previous sync left literal `<<<<<<<` / `>>>>>>>` lines in a
markdown file. `engram serve` refuses to start. Open the file, resolve
the conflict by hand, commit, and re-run.

## References

- ADR 005 - sync coordinator state machine + cross-vault contamination guard
- ADR 003 - system git CLI sync transport
- `docs/PHASE_2_PLAN.md` - implementation plan with risk + edge-case tables
- `src/engram/sync/coordinator.py` - reference implementation
