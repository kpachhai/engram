# Friend-share guide (bundle export / import)

Engram ships friend-share as point-in-time snapshots, NOT live
git-pull from a friend's vault. The rationale is documented in
[ADR 006](./adr/006-multi-vault-and-llm.md) D3: a friend's git
history is attacker-influenceable, and the bundle import gate is
the only place where engram can apply path-traversal refusal,
per-file 1 MB caps, per-bundle 4 GB streaming, YAML safe-load, and
`portability=block` filtering to friend-derived content.

## The three-step flow

### 1. Export from your vault

```bash
engram export \
  --vault personal \
  --portability portable \
  --output ~/share/personal-2026-05-05.tar.gz
```

Default portability filter is `portable` only. To include
`sensitive` thoughts, pass `--portability` repeatedly:

```bash
engram export \
  --vault personal \
  --portability portable \
  --portability sensitive \
  --output ~/share/personal-bundle.tar.gz
```

`block` thoughts are NEVER included regardless of the flag. The
export refuses if the per-vault daemon (or a legacy `engram serve
--no-daemon` process) is currently holding the per-vault lock. Stop
the daemon first with `engram daemon stop`, run the export, then
restart with `engram serve` (or the daemon will auto-spawn on the
next AI session).

### 2. Transfer

The bundle is a single `.tar.gz` that you can ship via any channel:
`scp`, USB stick, encrypted email, cloud storage. The bundle
contains:

* `manifest.json` at the archive root with the `bundle_id`
  (UUID-v7), `source_user`, `source_vault`, `exported_at`,
  `thought_count`, `portability_filter`, and `embedding_model`.
* `thoughts/<rel-path>.md` for each exported thought.

There's no encryption layer - the bundle is plain
markdown. Encrypt the transport channel if the contents warrant it.

### 3. Import into the recipient's vault

The recipient registers a new read-only vault in their per-user
config:

```yaml
vaults:
  - name: personal
    path: ~/.local/share/engram-vaults/personal
    role: primary
  - name: alice-shared
    path: ~/.local/share/engram-vaults/alice-shared
    role: read-only
```

Then runs:

```bash
engram import ~/share/personal-2026-05-05.tar.gz \
  --vault alice-shared \
  --allow-read-only
```

The `--allow-read-only` flag is required because importing into a
read-only-mounted vault is the typical friend-share case (the
recipient never directly captures into the friend's vault).

## What the importer enforces

Per ADR 006:

* Refuses if `manifest.schema_version != 1`
  (`bundle_import_error: schema_version_unsupported`).
* Cycle detection by `bundle_id` chain: walks every existing
  thought's `source: bundle:<id>` chain looking for the candidate
  `manifest.bundle_id`; refuses with `bundle_cycle_detected` if
  found.
* Streaming tar.gz reader; never loads the whole archive into RAM.
* Per-member validation: under `thoughts/`, no `..` segments, NFC
  unicode normalization, `\` → `/`, BOM stripped, YAML
  `safe_load` only.
* Refuses any member with `portability=block` (defense-in-depth at
  the import side; the exporter shouldn't include them but a
  malicious / mistaken friend push by hand still gets filtered).
* Stages all writes into
  `<vault>/.indexes/import-staging-<bundle_id>/`. Runs an
  id-collision pre-flight scan against existing thought ids; on ANY
  collision, the entire bundle is refused atomically before any
  merge into `thoughts/`.
* On success, walks the staging dir file-by-file and writes each
  into the target vault's `thoughts/`. Updates
  `<vault>/.indexes/bundle-import-<bundle_id>.json` after each file
  write so a crash mid-merge leaves an inspectable trail.
* Tags every imported thought with
  `source: bundle:<bundle_id> <- ...prior chain` so cycle detection
  in subsequent imports can walk the chain.

## LLM features and friend-share content

The synthesize tool (`synthesize_thoughts`) defaults
`include_friend_vaults=False` per ADR 006 D6. A crafted prompt
injection in a friend's body cannot reach the LLM context unless
the operator explicitly opts in:

```json
{
  "query": "what does alice think about embedding models?",
  "k": 10,
  "include_friend_vaults": true
}
```

When opted in, prompt assembly wraps each retrieved thought in
`<thought id="..." vault="..." source="bundle:..."> </thought>`
delimiters and the system prompt instructs the model to ignore
in-content directives. The citation post-validator strips any UUID
the LLM cites that wasn't in the actually-retrieved set. This is
a ratchet, not a guarantee: indirect prompt injection remains
unsolved at the model layer.

## Limitations (candidate future features)

* No `engram import-resume` subcommand exists today. Recovery from a
  partial-merge crash is manual: `engram doctor` surfaces the partial
  state with operator-runnable resume instructions; you remove the
  half-imported files in `<vault>/.indexes/import-staging-<bundle_id>/`
  manually and re-run `engram import` against the same bundle.
* Live git-pull from a friend's remote (D3 in ADR 006). Today's flow
  is one-shot bundle export + bundle import.
* Capability-token bundles (schema_version=2) - the v1 importer
  refuses anything else by design.

## See also

* [ADR 006](./adr/006-multi-vault-and-llm.md) D3, D6, D7 - friend-share design.
* [`MULTI_VAULT_SETUP.md`](./MULTI_VAULT_SETUP.md) - mounting the
  recipient's read-only vault.
* [`LLM_FEATURES.md`](./LLM_FEATURES.md) - the
  `include_friend_vaults` opt-in.
