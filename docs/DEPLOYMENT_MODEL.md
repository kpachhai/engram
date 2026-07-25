# engram Deployment Model

A common question: "How do I deploy engram so it's always available, like a SaaS?" Short answer: **you don't.** Engram is local-first by design, and the "always available" property comes from how the markdown vault syncs across machines, not from a central server. This doc explains why and what to do instead.

## The wrong mental model

If you're coming from Open Brain (Supabase + Edge Function) or Mem0 Cloud or Letta, your instinct is:

```
[your devices] -- network --> [some hosted engram MCP server] --> [your data in the cloud]
```

That's the SaaS model. **Engram does not do this.** There is no engram-as-a-service. There is no `engram serve --listen 0.0.0.0:8080` mode. The MCP wire is stdio at the AI-client boundary; internally (v0.5.0+) a per-vault Unix Domain Socket sits between the `engram serve` proxy and the daemon process, but the UDS is local IPC (mode 0o600, peer-cred-guarded) — not a network listener. Building a hosted engram contradicts the thesis (vendor lock-in, privacy boundary failures, cross-machine fragmentation are exactly the problems engram was designed to solve).

## The right mental model

```
[laptop]                  [desktop]                 [phone via SSH]
   │                         │                          │
   │  engram serve           │  engram serve            │  engram serve
   │     │                   │     │                    │     │
   ▼     ▼                   ▼     ▼                    ▼     ▼
[markdown vault]        [markdown vault]           [markdown vault]
       │                       │                          │
       └───── git push/pull to a remote you control ──────┘
                       (GitHub / Forgejo / Gitea / etc)
```

Each device runs its own `engram serve` against its own local copy of the markdown vault. The vault is a git repo. Sync is `git push` after a capture and `git pull` on serve startup. The "remote" is whatever git host you trust (GitHub, GitLab, Forgejo, Gitea, your own gitolite — anything that speaks git over SSH or HTTPS).

From v0.5.0, `engram serve` runs as a thin proxy that auto-spawns (or attaches to) a long-lived per-vault daemon. This is invisible at the AI-client boundary — same `engram serve` command, same stdio MCP — but it lets N concurrent AI sessions on one device share the vault. See [DAEMON_MODE.md](DAEMON_MODE.md) for the operator guide.

The "always available" property:

- **Offline:** every device has the full vault on disk. Loss of network does not lose memory.
- **Cross-machine:** `git pull` on each device picks up captures from the others within a sync cycle.
- **Cross-vendor:** if your AI client changes (Claude Code → some future tool), the data stays in your filesystem; only the MCP wiring changes.
- **Cross-employer:** personal vs work vaults live in physically separate repos on physically separate machines (see `docs/MULTI_MACHINE_SETUP.md` Vault Isolation).

## What about uptime, then?

Uptime concerns translate cleanly to local-first patterns:

| Concern | Local-first answer |
|---|---|
| "What if my MCP server is down?" | The AI client respawns the `engram serve` proxy automatically for stdio MCP servers (Claude Code does this). If the per-vault daemon also crashed, the proxy's spawn dance brings it back; the daemon's idle-shutdown contract cleans up sockets after the last proxy disconnects so there's nothing stale to recover from. |
| "What if my git remote is down?" | `engram serve` keeps working against the local vault. Captures queue up; the next successful `git push` flushes them. No data loss. |
| "What if my disk fails?" | Restore from any other machine that has cloned the vault. Or restore from the git remote. The markdown files ARE the data; nothing to recover from a database. |
| "What if I need to access my memory from a device I haven't set up yet?" | Clone the vault repo + install engram on the new device. Five minutes. See `docs/QUICKSTART.md` + `docs/MULTI_MACHINE_SETUP.md`. |

## The private-vault-repo pattern

A common convention: your personal vault lives in its own private git repo (the maintainer's is named `memex`). The repo holds:

- `thoughts/` — the markdown source of truth
- `engram.config.yaml` — vault config (committed)
- `.gitignore` — ensures `.indexes/` and `.engram/` are never committed

You DON'T deploy that repo anywhere. You:

1. Push it to a private git remote you control (e.g. `git@github.com:<your-username>/<your-vault-repo>.git`).
2. Clone it on every personal device you want AI memory on.
3. Run `engram serve` on each device against its local clone.
4. Let the sync coordinator handle push/pull.

The full setup walkthrough lives in `<your-meta-stack-repo>/workspace/engram/MEMEX_SETUP_GUIDE.md` (in the maintainer's planning repo).

## What about teams?

The same model scales to small teams via the `team-write` role. Each team member has their personal `memex` (or whatever they named it) repo PLUS the shared team-vault repo. Each member's `engram serve` mounts both. See `docs/TEAM_BRAIN_GUIDE.md`.

The team-vault repo is hosted on whatever git host the team trusts (GitHub Enterprise, self-hosted Forgejo, etc). The pre-receive hook enforces team policy at the git host. There is still no central engram server — every team member runs their own `engram serve` locally.

## What CAN be cloud-hosted

These pieces of the engram ecosystem live remotely:

- **The git remote** (GitHub / Forgejo / Gitea / GitLab) — for vault sync.
- **The pre-receive hook** (installed on the git host's bare-repo `hooks/` dir) — for team-vault policy enforcement.
- **An LLM provider** (optional; used by `summarize_thought` / `synthesize_thoughts`) — Anthropic / OpenAI / a self-hosted Ollama instance. Engram talks to it via API; the provider sees only the thoughts engram routes to it under the portability gate.
- **A FastEmbed model file** (cached locally after first download from HuggingFace) — the model itself lives in HuggingFace; engram just downloads + caches.

None of these are engram itself. Engram is the local binary.

## When you might want a hosted engram (future, conditional)

The architecture deliberately leaves room for a future enterprise tier:

- HTTP API alongside MCP stdio (for non-MCP clients).
- RBAC + SSO + audit log.
- Multi-tenancy (one engram instance serves many users via separate vaults).

These are designed-for, not built. The watch trigger is real-org adoption (a 50+ person team that wants compliance certifications). Until that happens, engram is local-first only. If/when it ships, the hosted tier composes on top of the same primitives — markdown SoT + git transport + MCP API — without replacing them.

## TL;DR

- **Don't deploy engram to the cloud.** It's local-first by design.
- **Sync the markdown vault via git** to a private remote you control.
- **Run `engram serve` on each device** that has an AI client.
- **A private vault repo is the right pattern.** See `MEMEX_SETUP_GUIDE.md` in the planning workspace for the full walkthrough.
- **"Always available" comes from "the data is on every device,"** not from "the server is in the cloud."

## See also

- `docs/QUICKSTART.md` — install + first capture (one machine).
- `docs/MULTI_MACHINE_SETUP.md` — git-based sync across personal devices.
- `docs/MULTI_VAULT_SETUP.md` — role taxonomy + per-user config.
- `docs/TEAM_BRAIN_GUIDE.md` — shared team vault with pre-receive policy enforcement.
- `docs/ARCHITECTURE.md` — the full data + sync model.
