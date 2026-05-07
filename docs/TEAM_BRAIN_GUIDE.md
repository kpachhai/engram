# engram Team Brain - Operator Guide

This guide walks an operator through bootstrapping a team-vault, adding
members, and the disaster-recovery flows.

## Prerequisites

* engram installed (`pip install engram-mcp` ≥ 0.4.0).
* GPG installed (`brew install gnupg` / `apt install gnupg`).
* A git remote you control (Forgejo / Gitea / GitLab CE / GitHub
  Enterprise / etc.) where you can install pre-receive hooks.
* All team members have:
  * the same `engram-mcp` version installed.
  * a GPG signing key generated.
  * git configured to sign commits (`git config commit.gpgsign true`).

## Step 1: Generate your GPG signing key (if you don't have one)

```bash
gpg --full-generate-key
# Choose:
#   - (1) RSA and RSA, or (9) ECC and ECC
#   - 4096 bits (RSA) or curve 25519 (ECC)
#   - 0 = key never expires (or set an expiry)
#   - your real name + email matching your git config user.email
```

After generation, find your primary fingerprint:

```bash
engram team-vault enroll-key
# Output: primary fingerprint: 1234567890ABCDEF1234567890ABCDEF12345678
```

Configure git to sign commits with this key:

```bash
git config --global user.signingkey 1234567890ABCDEF1234567890ABCDEF12345678
git config --global commit.gpgsign true
```

## Step 2: Bootstrap a team vault (steward only)

The steward is the team member running `engram team-vault setup`.
They become the first entry in `members.yaml` AND the first entry in
`stewards` in `team-policy.yaml`.

```bash
# 1. Create an empty git remote at <git-host>/<org>/<team-vault>.git
#    (Forgejo / Gitea / GitLab CE / GitHub Enterprise / etc.)

# 2. Clone it locally + run setup.
git clone git@your-git-host:org/team-vault.git ~/team-vaults/team-x
cd ~/team-vaults/team-x

engram team-vault setup ~/team-vaults/team-x \
  --remote git@your-git-host:org/team-vault.git \
  --steward-display-name "Alice"

# 3. Edit .engram/team-policy.yaml to set the team's allowlists.
#    For example:
#    allowed_prefixes:
#      - Postmortem
#      - Decision
#      - Lesson
#    accept_sensitive: false
$EDITOR .engram/team-policy.yaml

# 4. Commit + sign-off + push.
git add .
git commit -S -s -m "team-x: initial bootstrap"
git push -u origin main
```

## Step 3: Install the pre-receive hook on your remote

The hook is at `<engram-install>/team/server_hooks/pre_receive.py`.
Copy it to the bare-remote's `hooks/pre-receive` and make it
executable.

### Self-hosted Forgejo / Gitea

The bare repo lives at `/var/lib/gitea/repositories/<org>/<repo>.git/`
or wherever your install was configured. SSH into the host:

```bash
sudo cp pre_receive.py /var/lib/gitea/repositories/org/team-vault.git/hooks/pre-receive
sudo chmod +x /var/lib/gitea/repositories/org/team-vault.git/hooks/pre-receive
sudo chown gitea:gitea /var/lib/gitea/repositories/org/team-vault.git/hooks/pre-receive
```

### Self-hosted GitLab CE

GitLab uses a `custom_hooks/` subdirectory:

```bash
mkdir -p /var/opt/gitlab/git-data/repositories/@hashed/<hash>.git/custom_hooks
cp pre_receive.py /var/opt/gitlab/git-data/repositories/@hashed/<hash>.git/custom_hooks/pre-receive
chmod +x ...
```

### GitHub Enterprise

Pre-receive hooks are configured via the org admin UI:

* Org admin → Hooks → Pre-receive hooks → Upload `pre_receive.py`.
* Enable for the repo.

### Verify the hook is working

Have the steward push a violating commit and confirm refusal:

```bash
# Try to push a [Friction] thought against an allowlist of [Postmortem, Decision]
echo "---
id: 11111111-1111-1111-1111-111111111111
prefix: Friction
portability: portable
source: engram-test
created_at: '2026-01-01T00:00:00Z'
updated_at: '2026-01-01T00:00:00Z'
fingerprint: $(printf 'a%.0s' {1..64})
captured_by: $(engram team-vault enroll-key | grep -oE '[A-F0-9]{40}')
---
[Friction] should refuse" > thoughts/2026/01/test.md
git add . && git commit -S -s -m "test: should refuse"
git push
# Expected: pre-receive hook refuses with "prefix_not_allowed".
```

## Step 4: Add team members

Each member runs `engram team-vault enroll-key` to discover their
primary fingerprint, then sends it to the steward.

The steward runs:

```bash
engram team-vault add-member <member-fingerprint> \
  --members-yaml .engram/members.yaml \
  --policy-yaml .engram/team-policy.yaml \
  --display-name "Bob"

git add .engram/members.yaml
git commit -S -s -m "team-x: enroll bob"
git push
```

Members re-pull to see the updated roster:

```bash
cd ~/team-vaults/team-x
git pull
```

## Step 5: Members join the team vault

```bash
git clone git@your-git-host:org/team-vault.git ~/team-vaults/team-x

# Add the vault to your engram config:
$EDITOR ~/.config/engram/config.yaml
# Add a new entry under vaults:
#   - name: team-x
#     path: ~/team-vaults/team-x
#     role: team-write
#     remote_url: git@your-git-host:org/team-vault.git

# Restart engram serve.
```

## Step 6: Capture into the team vault

From an MCP client (Claude Code, etc.), capture with the explicit
target:

```json
{
  "name": "capture_thought",
  "arguments": {
    "content": "[Postmortem] Production deploy failed at 14:23. Root cause: ...",
    "metadata": { "vault": "team-x" }
  }
}
```

Or enable auto-routing in `~/.config/engram/config.yaml`:

```yaml
auto_route: true
routing_rules:
  - prefix: Postmortem
    target_vault: team-x
```

Then `[Postmortem]` captures land in `team-x` automatically (unless
`block` portability or explicit `vault:` arg overrides per pinned
invariants 1+2).

## Revocation

When a member leaves the team, the steward revokes their key:

```bash
# 1. Revoke the member's SSH access to the git remote (in the host's UI).

# 2. Mark the fingerprint as revoked in members.yaml.
engram team-vault revoke-key <member-fingerprint> \
  --members-yaml .engram/members.yaml \
  --policy-yaml .engram/team-policy.yaml \
  --reason "left the team 2026-05-30"
git add .engram/members.yaml
git commit -S -s -m "team-x: revoke <member>"
git push

# 3. The revoked member's local clone:
#    - cannot push (auth-fail at the remote).
#    - their engram doctor surfaces team_membership_revoked.
#    - their captures into team-x refuse with team_member_not_enrolled.
#    - their existing thoughts in team-x remain readable.
```

The revoked member's exit ramp:

```bash
engram team-vault unmount team-x \
  --user-config ~/.config/engram/config.yaml \
  --remove-local \
  --local-path ~/team-vaults/team-x
```

This removes the `team-x` entry from the member's `~/.config/engram/config.yaml`.
The `--remove-local` + `--local-path` pair also deletes the on-disk
clone (operator opt-in; omit both to keep the local files for archival).

## Disaster recovery

If the team-vault remote is permanently lost (host outage, account
compromise, etc.), there is no `engram team-vault restore` subcommand —
disaster recovery is plain git plus the existing `team-vault rebind`
ceremony:

```bash
# 1. Any steward with a healthy local clone repoints it at a new remote
#    and pushes the full history.
cd ~/team-vaults/team-x
git remote set-url origin git@new-host:org/team-vault.git
git push -u origin main

# 2. Each member re-points their local clone via team-vault rebind.
engram team-vault rebind team-x \
  --user-config ~/.config/engram/config.yaml \
  --remote git@new-host:org/team-vault.git \
  --local-clone ~/team-vaults/team-x
```

`team-vault rebind` updates the member's `~/.config/engram/config.yaml`
entry for `team-x` AND (when `--local-clone` is set) runs
`git remote set-url` on the local checkout so subsequent `engram serve`
syncs against the new remote.

## Troubleshooting

### `engram doctor` shows `team_member_not_enrolled` FAIL

Your fingerprint isn't in the team's `members.yaml`. Ask a steward
to run `engram team-vault add-member <your-fingerprint>` and re-pull.

### `engram doctor` shows `team_pending_pushes` INFO

The push queue has retries waiting for the remote to come back. This
is informational; pushes resume automatically when the remote is
reachable. If it persists for >24 hours, check your network + auth.

### `engram doctor` shows `team_membership_revoked` FAIL

You've been removed from the team. Run `engram team-vault unmount
--remove-local team-x` to clean up your local state. Your already-
captured thoughts in your personal vault remain.

### Push refuses with `attribution_committer_mismatch`

Your `captured_by:` field doesn't match the GPG-signed committer
fingerprint. Common cause: hand-edited markdown. Re-capture via
`capture_thought` MCP tool which sets the field correctly, or set
`captured_by:` to your primary fingerprint.

### Push refuses with `prefix_not_allowed`

The team policy doesn't allow that prefix in this vault. Capture
into your personal vault instead, or ask a steward to update
`team-policy.yaml`.

### Push refuses with `indexes_path_refused`

You accidentally `git add`ed a `.indexes/` file. Reset:

```bash
git rm --cached -r .indexes/
git commit -S -s -m "fix: drop .indexes/ from index"
```

The canonical `.gitignore` (written by `setup`) prevents this; if
you removed those entries, restore them.

## See Also

* `docs/adr/007-team-brain.md` - design decisions.
* `docs/archive/phases/PHASE_4_CODE_COMPLETE.md` - exit-criteria evidence.
* `docs/archive/phases/PHASE_4_PLAN.md` - the 22-step implementation plan.
* `docs/MULTI_VAULT_SETUP.md` - the multi-vault role taxonomy.
