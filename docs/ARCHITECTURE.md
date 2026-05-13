# engram - Architecture

How the storage, sync, and MCP layers fit together. Read this if you want to understand engram's internals, contribute, or fork.

## The thesis in one sentence

Markdown files are the source of truth, SQLite + sqlite-vec is a regenerable index, git is the sync mechanism, MCP is the API.

Everything else is consequences of those four choices.

## Components

```
┌──────────────────────────────────────────────────────────────────────┐
│                         MCP-aware AI client                          │
│              (Claude Code, Claude Desktop, custom)                   │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ stdio MCP (JSON-RPC over stdin/stdout)
┌─────────────────────────────▼────────────────────────────────────────┐
│                  engram serve  (proxy mode, default)                 │
│       Stateless byte shuffler: stdio ↔ UDS. Auto-spawns the          │
│       daemon on first invocation; attaches on subsequent ones.       │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ Unix Domain Socket
                              │ <vault>/.indexes/engram.sock (0o600)
                              │ + SO_PEERCRED / getpeereid
┌─────────────────────────────▼────────────────────────────────────────┐
│        engram daemon  (one process per vault; long-lived)            │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  FastMCP server (build_multivault_server)                      │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │  │
│  │  │capture_thought│  │search_thoughts│  │summarize/synthesize │  │  │
│  │  │(routing+gate)│  │(per-vault)   │  │(LLM, opt-in)         │  │  │
│  │  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘  │  │
│  │         │                 │                     │              │  │
│  │  ┌──────▼─────────────────▼─────────────────────▼───────────┐  │  │
│  │  │              VaultRegistry                               │  │  │
│  │  │  name -> (VaultStorage, role, SyncCoordinator)           │  │  │
│  │  └──────┬───────────────────────────────────────────────────┘  │  │
│  └─────────┼──────────────────────────────────────────────────────┘  │
│            │                                                         │
│  ┌─────────▼──────────────────┐  ┌──────────────────────────────┐    │
│  │   VaultStorage (per vault) │  │   SyncCoordinator (per vault)│    │
│  │   Markdown SoT + SQLite    │  │   debounce + commit + push   │    │
│  │   sqlite-vec ANN search    │  │   persistent push queue      │    │
│  │   FastEmbed (BAAI/bge-     │  │   conflict detection         │    │
│  │     small-en-v1.5)         │  │                              │    │
│  └────────────────────────────┘  └─────────────┬────────────────┘    │
└──────────────────────────────────────────────────┼───────────────────┘
                                                   │ git push/pull
                                            ┌──────▼────────┐
                                            │  Git remote   │
                                            │  (GitHub etc) │
                                            └───────────────┘
```

The proxy / daemon split (introduced in v0.5.0) lets **N concurrent AI
sessions** attach to the same vault. Each ``engram serve`` invocation
spawns a thin proxy that auto-spawns or attaches to the per-vault
daemon. The MCP wire format observed by the AI client is unchanged.

The ``engram serve --no-daemon`` flag reverts to the legacy
single-process model for embedded use cases. See
[``DAEMON_MODE.md``](DAEMON_MODE.md) for the operator guide and
[``adr/008-daemon-mode.md``](adr/008-daemon-mode.md) for the design
rationale.

## On-disk layout

A vault is a directory:

```
~/.local/share/engram/personal/         # vault root (you choose the path)
├── engram.config.yaml                  # vault config (committed)
├── .gitignore                          # ensures .indexes/ is never committed
├── README.md                           # operator-facing stub
├── thoughts/                           # markdown SoT
│   ├── lesson/
│   │   └── 2026/05/2026-05-04-engram-architecture-<UUID>.md
│   ├── decision/
│   ├── friction/
│   └── ...                             # one subdirectory per canonical prefix
├── .indexes/                           # regenerable; gitignored
│   ├── engram.db                       # SQLite (sqlite-vec virtual table)
│   ├── engram.db-wal
│   ├── engram.db-shm
│   ├── engram.lock                     # advisory flock (held by daemon)
│   ├── engram.sock                     # UDS (daemon listener; mode 0o600)
│   ├── engram.spawn.lock               # serializes concurrent spawn dances
│   ├── engram.state.json               # daemon PID + hostname + config snapshot
│   └── engram.log                      # daemon log (rotated)
└── .engram/                            # local-only operational state (gitignored)
    ├── identity.local                  # per-machine identity overrides
    ├── push-queue.local                # persistent push queue (team vaults)
    └── orphans/                        # auth-failure orphan tarballs
```

Team-vault adds:

```
.engram/team-policy.yaml                # checked-in: prefix allowlist, embedding model lock, stewards
.engram/members.yaml                    # checked-in: enrolled GPG fingerprints
.engram/setup_complete                  # sentinel; idempotency for setup
```

## Frontmatter schema

Every thought is one markdown file with strict YAML frontmatter:

```yaml
---
schema_version: 1                        # forward-compat
id: 01956f63-1234-7890-abcd-ef1234567890 # UUID-v7 (timestamp-prefix)
prefix: Lesson                           # one of 15 canonical values
portability: portable                    # portable | sensitive | block
source: alice                            # capture source (user / agent / migration)
created_at: 2026-05-04T14:23:00Z         # immutable
updated_at: 2026-05-04T14:23:00Z         # bumps on edit
fingerprint: 1c2a... (sha256)            # 64 hex; canonical body fingerprint
tags: [hedera, debugging]                # optional
vault: personal                          # vault attribution (multi-vault deployments)
captured_by: 1234567890ABCDEF...         # GPG fingerprint (team vaults only)
---

The body content. Plain markdown.
```

The body is human-edited. Edits made in a text editor, in Obsidian, or anywhere else are detected on next `engram serve` startup (fingerprint compare against SQLite); the index re-syncs.

## Storage Flow A: capture

The capture flow has a strict ordering contract:

```
1. Validate inputs (prefix, portability, fingerprint).
2. Run team-vault capture gate (if target is team-write):
   a. Refuse if vault is read-only.
   b. Assert local GPG fingerprint is in members.yaml.
   c. Run policy.refuse_or_pass(thought).
   d. Stamp thought.captured_by with the operator's GPG fingerprint.
3. Write markdown file (atomic: tmp + fsync + rename).
4. Insert SQLite row + embedding (one transaction).
5. Optional sync: enqueue on the SyncCoordinator (debounce + commit + push).
```

**Why markdown first:** if SQLite corruption happens later, the markdown is the truth; we can rebuild the index. If we wrote SQLite first and the markdown failed, we'd have a row with no file.

**Embedding is failure-tolerant:** if FastEmbed crashes, the thought is captured with `embedding_status='pending'` and `engram doctor --repair` regenerates it later.

## Multi-vault: roles + routing

A vault has one of three roles:

| Role | Writes? | Cross-vault search? | LLM tools? |
|------|---------|---------------------|------------|
| `primary` | yes (default capture target) | yes | yes |
| `read-only` | refused with `VaultReadOnlyError` | yes (with attribution) | yes (per portability gate) |
| `team-write` | yes (after capture gate + push hook) | yes (with attribution) | yes (per portability gate) |

At most one `primary`; arbitrary number of `read-only` and `team-write`.

The routing dispatcher (`engram.team.routing.resolve_target_vault`) decides where a capture lands when the user has multiple vaults mounted:

1. **`portability=block`** → primary (always; pinned invariant).
2. **`portability=sensitive`** + target's `accept_sensitive=False` → primary.
3. **Explicit `meta.vault` arg** → that name.
4. **`auto_route=true` + matching routing rule** → rule's target.
5. **Otherwise** → primary.

## Sync coordinator state machine

Per-vault git sync runs as one asyncio task. State transitions are validated against an explicit table:

```
IDLE ─enqueue─▶ DEBOUNCING ─timer─▶ COMMITTING ─ok─▶ PUSHING ─ok─▶ IDLE
                                    │                │
                                    ▼ retry          ▼ auth-fail
                              MANUAL_RESOLUTION    AUTH_REQUIRED
```

The full transition table is in `src/engram/sync/coordinator.py:ALLOWED_TRANSITIONS`. Disallowed transitions raise `SyncError` rather than silently advancing.

**Debouncing:** captures within `debounce_window_seconds` (default 60s) are coalesced into a single commit. A `max_deferral_seconds` ceiling (default 5min) ensures continuous activity still flushes.

**Push retry + persistent queue:** team-write vaults keep a persistent push queue at `<vault>/.engram/push-queue.local`. Engram restart replays pending pushes from disk. Auth-failure during push moves affected files to an orphan tarball under `<personal>/.engram/orphans/` for the operator's `engram orphan-recover` flow.

## Two-layer security boundary (team vaults)

The team-vault model uses two independent enforcement layers:

| Concern | Client-side gate | Server-side hook |
|---------|------------------|------------------|
| `block` portability | refuses at routing dispatcher | refuses at push (defense-in-depth) |
| Member enrollment | refuses at capture gate | n/a (push fails on its own when fingerprint not in members.yaml) |
| Prefix allowlist | refuses at capture gate | refuses at push |
| Source allowlist | refuses at capture gate | refuses at push |
| `captured_by` ↔ committer match | n/a (set by gate) | refuses at push |
| `.indexes/` containment | `.gitignore` | refuses at push |
| Force-push refusal | engram strips `--force` flags | `denyNonFastForwards` + hook check |
| Steward-only mutation of policy / members | n/a | refuses at push if committer not in stewards |

A client-side bypass (older client, hand-edited markdown, forked engram) does NOT breach the boundary because the server-side hook catches it.

The hook is a stdlib-only Python 3.10+ script at `src/engram/team/server_hooks/pre_receive.py`. It is COPIED to the team-vault remote's `hooks/pre-receive` by the operator at setup time; engram does not require Python on the git host (the hook can run on any host that has Python 3.10+).

## Process model: proxy + daemon

From v0.5.0 onward, ``engram serve`` runs in **proxy mode** by default:
a short-lived process that connects to (or auto-spawns) a per-vault
**daemon** over a Unix Domain Socket. The daemon owns the long-lived
resources (``VaultLock``, ``VaultStorage``, ``SyncCoordinator``,
``FastEmbedProvider``, ``FastMCP``) and accepts N concurrent proxy
connections sharing one vault.

### Why this exists

Pre-v0.5.0, ``engram serve`` held the per-vault advisory lock for its
lifetime. A second concurrent session against the same vault failed
with ``LockError``. Users hit this regularly with two Claude Code
sessions open against the same memex vault. Daemon mode resolves it
without requiring any MCP config changes — the proxy IS still
``engram serve`` from the AI client's POV.

### Topology

- **One daemon per vault.** The daemon for a primary vault also mounts
  any configured read-only or team-write extras (Phase 3 multi-vault
  semantics unchanged).
- **N proxies per daemon.** Each AI session gets its own proxy
  process; the proxy is stateless byte-shuffling between stdio (toward
  the AI) and UDS (toward the daemon). ~200 LOC.
- **No network listener.** UDS is local IPC, mode 0o600, plus
  ``SO_PEERCRED`` (Linux) / ``getpeereid`` (macOS) on every accepted
  connection. Same-UID is the trust boundary.
- **Auto-spawn + auto-idle-shutdown.** The first ``engram serve``
  forks the daemon and waits for ``ready\n`` on a readiness pipe.
  After the last proxy disconnects, the daemon idles for
  ``daemon.idle_shutdown_seconds`` (default 60 min), then exits
  cleanly. The next ``engram serve`` re-spawns it transparently.
- **``--no-daemon`` escape hatch.** ``engram serve --no-daemon``
  reverts to the legacy single-process stdio model for one-shot
  scripts, embedded use cases, or debugging.

### What changes for callers

| Caller | Pre-v0.5.0 behavior | v0.5.0+ behavior |
|---|---|---|
| AI client (Claude Code etc) | stdio MCP server | stdio MCP server (unchanged) |
| Operator running `engram serve` | one process per session | proxy attaches to daemon (or auto-spawns) |
| Concurrent sessions | second one hits `LockError` | N proxies share one daemon |
| First-session latency | direct startup (~1-2s for FastEmbed) | proxy attach instant; daemon spawn ~1-2s on cold start |
| Mutual exclusion with `--no-daemon` | n/a | `--no-daemon` and the daemon both want `VaultLock`; whichever holds it first wins |

See [``DAEMON_MODE.md``](DAEMON_MODE.md) for the operator guide
(start/stop/status/logs, config knobs, troubleshooting) and
[``adr/008-daemon-mode.md``](adr/008-daemon-mode.md) for the design
rationale (per-vault topology, UDS-over-HTTP, the FastMCP dispatch
compat shim).

## MCP API surface

Seven tools, stable for the v1.x lifetime:

| Tool | Purpose | Side effects |
|------|---------|--------------|
| `capture_thought` | Write a new thought | markdown + SQLite + (optional) embedding + (optional) sync enqueue |
| `search_thoughts` | Semantic search (top-k) | none |
| `list_thoughts` | Filtered + sorted + paginated list | none |
| `thought_stats` | Aggregate counts | none |
| `fetch` | Lookup by id | none |
| `summarize_thought` | LLM-mediated single-thought summary (opt-in) | LLM call (per portability gate) |
| `synthesize_thoughts` | LLM-mediated cross-vault RAG (opt-in) | LLM call (per portability gate) |

**API stability commitment:** these signatures and field shapes are stable for the v1.x lifetime. Only non-breaking additions (new optional fields, new optional filter dimensions, new tools) are permitted. Breaking changes warrant v2.0.

## Identity model

Three identity surfaces, depending on context:

1. **`default_user`** — for personal vaults, the free-form source attribution string. Resolved from per-user config, falling back to `~/.config/devkit/identity.json`'s `github_username` field, falling back to `$USER`.
2. **`captured_by`** — for team vaults, the GPG primary fingerprint (40 hex; canonical upper-case). Set by the team-vault capture gate before write.
3. **`stewards`** — list of GPG fingerprints in `team-policy.yaml` with disaster-recovery + policy-mutation + member-mutation + redaction permission.

GPG identity is discovered via `gpg --list-secret-keys --with-colons`; the colon-format walker resolves subkeys back to their primary so `git verify-commit` outputs map to the canonical fingerprint stored in `members.yaml`.

## LLM features (opt-in)

Engram's five core MCP tools are deterministic — no LLM calls. Two additional tools layer LLM-mediated features on top:

- `summarize_thought(id)` — compresses a single thought via the configured LLM provider with a citation post-validator.
- `synthesize_thoughts(query, k, filter)` — RAG-style cross-vault synthesis. Aggregates top-k from all mounted vaults (per portability gate), wraps each in a delimited block, calls the LLM with an anti-injection system prompt, validates citations.

Provider abstraction: Anthropic / OpenAI / Ollama / llama.cpp / generic OpenAI-compatible. Configured in `~/.config/engram/config.yaml` `llm:` block. Hard constraints:

- `portability=block` thoughts NEVER reach an LLM regardless of provider locality.
- `portability=sensitive` thoughts only reach LOCAL providers (Ollama / llama.cpp).
- A daily cost cap is enforced per vault (defaults to $5/day; configurable).
- LLM failures are non-fatal — the core tools keep working.

See [docs/LLM_FEATURES.md](LLM_FEATURES.md) for the full spec.

## Doctor + diagnostics

`engram doctor` runs probes that map 1:1 to startup-time checks:

- Git version, branch alignment, conflict markers, cloud-sync drift, `.indexes/` gitignore, signed-commits, autocrlf, submodule containment, GPG agent reachability.
- Multi-vault: at-most-one-primary, vault-path collision, embedding-model uniformity.
- LLM: provider reachability, cost-cap proximity.
- Team-vault: member enrollment, pending push queue depth, membership revocation, policy-violation orphans, branch-drift, stale serve config.

Each probe maps to a stable string code (see `src/engram/diagnostics/check_codes.py`). Codes are part of the public API — operators script against them.

## Embedding model

`BAAI/bge-small-en-v1.5` is the pinned default. 384-dim. ~130MB model file. Runs on CPU in milliseconds via FastEmbed. Local; no API calls.

The embedding model is recorded in the SQLite settings table on first capture. Subsequent opens with a different model raise `EmbeddingModelMismatch` until you run `engram reindex --full --model <new-model>`.

Multi-vault deployments require all vaults to agree on the embedding model — cross-vault similarity scores are not comparable across embedding models.

## What's regenerable, what's not

| Layer | Regenerable | Source of truth |
|-------|-------------|------------------|
| Markdown files | NO (source of truth) | Themselves |
| SQLite index | YES (`engram reindex`) | Markdown |
| Embeddings | YES (`engram reindex` or `doctor --repair`) | Embedding model + body |
| Push queue | NO (durable on disk) | Itself |
| Orphan tarballs | NO | Themselves |
| Members.yaml + team-policy.yaml | NO (checked-in) | The team's git remote |

If `.indexes/engram.db` corrupts, you can rebuild it in a single command. If a markdown file goes missing, it's gone — but `git log` recovers the prior version.

## File-naming conventions

`thoughts/<prefix-lowercase>/<YYYY>/<MM>/<YYYY-MM-DD>-<slug>-<UUID-tail>.md`

Example: `thoughts/lesson/2026/05/2026-05-04-engram-architecture-1234abcd.md`

The slug is derived from the first ~80 characters of the body; the UUID tail keeps filenames unique even when slugs collide. Year/month subdirectories keep the per-prefix dir from getting unmanageably large at 10K+ thoughts.

## Frontmatter schema drift handling

When engram reads a markdown file, the frontmatter is validated against the strict Pydantic schema. Drift falls into four categories:

1. **Missing required field:** structured warning, file is skipped (not indexed).
2. **Schema-version mismatch:** missing `schema_version` is treated as `1` for back-compat (a hand-edit might omit it).
3. **Unknown extra field:** preserved on round-trip (write-side serializer keeps the field, allows future schema additions).
4. **Type violation:** structured warning, file is skipped.

All drift surfaces in `engram doctor` so operators can clean up.

## Forward compatibility

The `schema_version` integer in every thought's frontmatter lets future versions of engram up-convert old files on read. A markdown file written by today's engram (`schema_version: 1`) MUST be readable by every future engram. New schema versions add fields with safe defaults; existing fields are never removed.

This is the contract that makes "your data outlives every vendor" actually true: even if engram itself is forked or replaced, the markdown corpus loads in any future tool that respects the schema.

## See also

- `docs/superpowers/specs/2026-05-04-engram/` (in the planning repo) — the full spec including roadmap, security model, operations playbook, competitive landscape, and migration plan.
- `docs/adr/` — Architecture Decision Records for each major design choice.
- `docs/QUICKSTART.md` — five-minute install + first capture.
- `docs/USE_CASES.md` — five concrete personas with example flows.
- `docs/COMPARISONS.md` — engram vs Mem0, Letta, basic-memory, Open Brain, Obsidian, engraph.
- `docs/MULTI_VAULT_SETUP.md` — role taxonomy + per-user config layout.
- `docs/MULTI_MACHINE_SETUP.md` — git-based sync across personal devices.
- `docs/FRIEND_SHARE_GUIDE.md` — bundle export / import flow.
- `docs/TEAM_BRAIN_GUIDE.md` — shared team vault setup + policy + revocation.
- `docs/LLM_FEATURES.md` — opt-in LLM-mediated tools.
- `docs/DAEMON_MODE.md` — daemon-mode operator + migration guide (v0.5.0+).
