# Multi-vault setup

Engram supports multi-vault hosting: one engram process serves N
vaults under different roles, including a `team-write` role for
shared team vaults. From v0.5.0 onward, the "engram process" that
holds your mounted vaults is the per-vault **daemon** (auto-spawned
by `engram serve`); each AI session attaches as a thin proxy. From
the multi-vault POV nothing changes — the daemon for a primary
vault still mounts read-only and team-write extras the same way
the pre-v0.5.0 single-process serve did.

This guide covers the per-user config layout, role semantics, and
the offline-preparation flow (so you don't get caught downloading
an embedding model the first time you try to capture a thought
without network).

## Concepts

* **Primary vault**: the vault that accepts captures by default.
  Exactly one primary per daemon (i.e. per `engram serve` invocation
  in `--no-daemon` mode, or per the per-vault daemon in proxy mode).
* **Read-only vault**: a mirror of someone else's vault, imported
  via `engram import`. Search includes it (with attribution); writes
  refuse with `VaultReadOnlyError`.
* **Team-write vault**: a shared team vault that accepts
  writes from multiple operators after passing a capture-time
  client-side gate (member-enrollment + policy refuse-or-pass) AND
  a push-time server-side `pre-receive` hook. Requires `remote_url`.
  See `TEAM_BRAIN_GUIDE.md` for the full setup walkthrough.
* **Vault registry**: in-process resolver that binds each vault's
  logical `name` to its open storage + role + sync coordinator. The
  registry is the canonical enforcement point for two invariants:
  no two vaults realpath-collide, and at most one is primary.

## Role taxonomy

| Role | Writes? | Cross-vault search? | LLM tools? |
|------|---------|---------------------|-------------|
| `primary` | yes (default capture target) | yes (own only) | yes |
| `read-only` | refused with VaultReadOnlyError | yes (with attribution) | yes (per portability gate) |
| `team-write` | yes (after capture gate + push hook) | yes (with attribution) | yes (per portability gate) |

## Per-user config layout

Edit `~/.config/engram/config.yaml` with the `vaults:` list:

```yaml
default_user: your-username
vaults:
  - name: personal
    path: ~/.local/share/engram-vaults/personal
    role: primary
  - name: alice-shared
    path: ~/.local/share/engram-vaults/alice-shared
    role: read-only
  - name: work-readonly
    path: ~/.local/share/engram-vaults/work-mirror
    role: read-only

aggregator:
  min_per_vault_results: 3
  aggregate_timeout_seconds: 5.0
  force_sequential: false

llm:
  provider: ollama
  model: llama3.2
  base_url: http://localhost:11434/v1
  daily_cost_cap_usd: 0.0  # local provider; cap not enforced
  max_input_tokens: 8000
  request_timeout_seconds: 60.0
```

Validation rules (enforced at config load):

* Exactly zero or one primary vault.
* No two `path:` values resolve to the same realpath (symlinks are
  followed).
* No two vault `name:` values collide (case-sensitive exact match).

## First-time offline preparation

The default embedding model
(`BAAI/bge-small-en-v1.5`) downloads on first use. If you plan to go
offline (work travel, demo on airplane), pre-download it:

```bash
engram doctor --download-model
```

The doctor command walks every configured vault and:

* Refuses if the embedding model isn't yet on disk and you're offline.
* Surfaces which vaults are mounted, their role, and the active
  aggregator mode (`ATTACH` vs `SEQUENTIAL`).
* WARNs if any read-only vault declares its own per-vault `llm:`
  block (the resolver ignores such blocks; see
  [`LLM_FEATURES.md`](./LLM_FEATURES.md)).

## Mounting a friend's vault as read-only

Two flows exist:

1. **Bundle import** (recommended; see
   [`FRIEND_SHARE_GUIDE.md`](./FRIEND_SHARE_GUIDE.md)). The bundle
   import gate enforces path-traversal refusal, per-file 1 MB cap,
   per-bundle 4 GB streaming, YAML safe-load, and
   `portability=block` filtering.
2. **`engram clone-vault <url> <local-path>`**. This runs
   `git clone --no-checkout` then removes
   `.git/hooks/` before the checkout phase fires them. After
   cloning, register the vault in `~/.config/engram/config.yaml`
   under `role: read-only`.

## Cross-vault search

The MCP `search_thoughts` tool defaults to the primary vault. To
search across all mounted vaults, pass `filter.vault = "*"`:

```json
{
  "query": "embedding model drift",
  "k": 10,
  "filter": { "vault": "*" }
}
```

The result set carries `vault` attribution per row. The aggregator
applies the portability invariant
(`block` NEVER appears regardless of any flag; `sensitive` only when
`include_sensitive=True`) at the SQL push-down layer.

## Per-vault floor

The aggregator's `min_per_vault_results` (default 3) guarantees
every mounted vault contributes at least three results to a
cross-vault search regardless of similarity ranking. This prevents
a small vault from drowning under a much larger primary's results.
The remaining slots up to `k` fill from the global similarity
ranking.

## Doctor checks the multi-vault config

`engram doctor` runs the eight multi-vault checks per `docs/adr/006-multi-vault-and-llm.md`:

| Code | When it fires |
|---|---|
| `multiple_primary_vaults` | FAIL when more than one vault declares `role: primary` |
| `vault_path_collision` | FAIL when two vaults resolve to the same realpath |
| `embedding_model_mismatch_across_vaults` | FAIL when vaults disagree on model + dim |
| `aggregator_mode` | INFO row showing ATTACH or SEQUENTIAL |
| `llm_provider_reachable` | WARN when configured provider doesn't respond |
| `llm_daily_cost_cap_approached` | WARN at >=80% of daily cap |
| `read_only_vault_declares_llm` | WARN when a read-only vault has an `llm:` block |
| `friend_vault_block_thought_present` | FAIL when a read-only vault carries `portability=block` |

## Local two-machine smoke test

The single-machine smoke test (clone the same source vault into
two directories, run `engram serve` against each, exchange captures
via git push/pull) extends naturally to the multi-vault case:
configure both machines with the same `vaults:` list, where one
machine's primary is the other machine's read-only mirror. Captures
still flow through git; multi-vault adds that doctor and search now
see the read-only mirror as a separate vault with attribution.

## See also

* [ADR 006](./adr/006-multi-vault-and-llm.md) - multi-vault design rationale.
* [DAEMON_MODE.md](DAEMON_MODE.md) - the per-vault daemon process that owns the mounted-vault set.
* [`FRIEND_SHARE_GUIDE.md`](./FRIEND_SHARE_GUIDE.md) - bundle export
  / import workflow.
* [`LLM_FEATURES.md`](./LLM_FEATURES.md) - LLM-mediated tools.
* [`MULTI_MACHINE_SETUP.md`](./MULTI_MACHINE_SETUP.md) - single-vault
  multi-machine sync baseline this builds on.
