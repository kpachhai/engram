# Migrating from Open Brain to engram

**Audience:** anyone running an Open Brain (OB1) deployment on Supabase who wants to move to engram. This guide assumes you have shell access to your machine, basic comfort with environment variables, and (eventually) write access to your Supabase project for the decommissioning step.

**Time required:** 30-60 minutes for a typical 2000-thought corpus, plus 14 days of trial running both systems before decommissioning.

**The migration is idempotent.** A failed or partial run can be resumed safely. The migration NEVER mutates your Open Brain database.

## What the migration does

`engram migrate-from-open-brain` paginates through every thought in your Open Brain MCP endpoint, generates a fresh UUID-v7 for each (preserving the original Open Brain ID in the `legacy_id` frontmatter field), parses prefixes, computes engram-canonical fingerprints, generates embeddings locally with FastEmbed, and writes one markdown file per thought to your engram vault. A `migration-report.json` lands at the vault root with full evidence.

After migration:
- Your Open Brain corpus is untouched.
- Your engram vault has one markdown file per Open Brain thought, with all metadata preserved.
- Search results in engram match search results in Open Brain (same content, same prefixes, same portability tags).

## Pre-migration checklist

Before running the migration, work through this checklist. Skipping any step risks data loss.

### 1. Take a Supabase snapshot

This is your rollback safety net. Even if both Open Brain AND engram end up in a bad state, the snapshot can be restored to a fresh Supabase project.

In the Supabase dashboard for your Open Brain project:

* Database → Backups → "Create backup" (or use the project's automatic-backup retention if it covers the migration window).
* Alternatively, table-level export: SQL Editor → `COPY public.thoughts TO STDOUT WITH CSV HEADER` redirected to a local file.

Confirm the snapshot exists and is non-empty before proceeding. Without it, the `--confirm-supabase-snapshot-taken` flag is dishonest.

### 2. Confirm Open Brain is reachable

```bash
# Replace <ref>, <key> with your values.
curl -s -X POST "https://<ref>.supabase.co/functions/v1/open-brain-mcp" \
  -H "Content-Type: application/json" \
  -H "x-brain-key: <key>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

Expected: a JSON response with `result.protocolVersion` (or similar). A 401/403 means your access key is wrong; a 404 means the function URL is wrong.

### 3. Install engram

```bash
pip install engram-mcp
# Or with uv (recommended):
uv tool install engram-mcp

engram --version          # confirms install
```

Python 3.11+ on macOS or Linux. Windows works under WSL; native Windows is best-effort.

### 4. Initialize a fresh engram vault

Pick a path you want your memory to live at:

```bash
engram init ~/.local/share/engram/personal
```

This creates `thoughts/` (one subdir per canonical prefix), `.indexes/`, `engram.config.yaml`, `.gitignore`, and a stub `README.md`. It refuses to overwrite an existing vault.

### 5. Pre-download the embedding model

```bash
engram doctor --download-model
```

This pulls `BAAI/bge-small-en-v1.5` (~130MB) from HuggingFace into the vault's local cache. The migration step then uses the cached model. If you skip this, the migration's first capture also triggers the download — which works but adds 1-2 minutes to the run.

### 6. Verify the doctor is all-green on the empty vault

```bash
engram doctor
```

Expected output: every check `[OK]`. If anything is `[FAIL]` or `[WARN]`, the message tells you the remediation. **Don't proceed with migration until the doctor is green.**

### 7. Set the `default_user` in your per-user config

Engram needs to know what to set as the `source` field on thoughts that don't carry one from Open Brain. Either:

```bash
mkdir -p ~/.config/engram
cat > ~/.config/engram/config.yaml <<EOF
default_user: <your-handle>
vaults:
  - name: personal
    path: ~/.local/share/engram/personal
    role: primary
EOF
```

Or rely on the devkit identity convention: if `~/.config/devkit/identity.json` exists with a `github_username` field, engram reads it automatically.

## Run the migration

### Dry-run first (recommended)

```bash
# Set the access key in env (preferred over --key for ps-aux safety).
export OPEN_BRAIN_KEY='<your-OB-access-key>'

engram migrate-from-open-brain \
  --url 'https://<ref>.supabase.co/functions/v1/open-brain-mcp' \
  --vault personal \
  --dry-run
```

`--vault personal` references a vault you've configured in `~/.config/engram/config.yaml` `vaults:` list (NOT a path — engram resolves the path from the vault's name).

What `--dry-run` does:
- Connects to Open Brain.
- Pages through every thought.
- Parses, transforms, and validates each thought.
- Writes NOTHING to disk.
- Generates `migration-report.json` showing what WOULD have been migrated.

Inspect the report before the real run. Look for:
- `totals.enumerated` — does the count match what you expect from Open Brain?
- `errors[]` — any thoughts that failed to parse? Investigate before proceeding.
- `fallback_assignments` — how many thoughts got the default `Note` prefix because no `[Prefix]` was found? If the count is high, your Open Brain corpus may have non-standard prefixes; review the migration report to decide whether to accept or fix.

### Real run

When the dry-run looks good (assuming `OPEN_BRAIN_KEY` is still set in your environment):

```bash
engram migrate-from-open-brain \
  --url 'https://<ref>.supabase.co/functions/v1/open-brain-mcp' \
  --vault personal \
  --confirm-supabase-snapshot-taken
```

The `--confirm-supabase-snapshot-taken` flag is REQUIRED for the real run. Without it, the migration refuses to start unless `--dry-run` is also set.

### Devkit references shortcut

If `~/.config/devkit/references.json` has an `open_brain_mcp_url` field (the maintainer's dotfiles convention), the `--url` and `--key` are read from there:

```bash
engram migrate-from-open-brain \
  --vault personal \
  --confirm-supabase-snapshot-taken
```

### Useful flags

| Flag | When to use |
|---|---|
| `--dry-run` | Always; run before the real migration. |
| `--limit <N>` | Test the pipeline on the first N thoughts. |
| `--prefer-legacy-id-match` | Re-migrate after edits in Open Brain. Matches by `(legacy_id, source)` and updates existing engram thoughts in place rather than creating duplicates. |
| `--report-path <path>` | Write the report to a non-default location. Default: `<vault>/migration-report.json`. |

**Resume after partial failure:** there is no separate `--resume` or `--append` flag. The migration is idempotent — already-imported thoughts are skipped via `(fingerprint, source, created_at)` triple-match on every run. Just re-run the same command after a partial failure.

## Performance expectations

For a typical Open Brain corpus (~2000 thoughts, ~5MB total content):

- Connection + enumeration: under 1 minute.
- Per-thought transform + embed + write: ~1-2 seconds (dominated by FastEmbed CPU embedding).
- Validation sample (10 thoughts): under 10 seconds.
- **Total: 30-60 minutes on a 2024-era laptop.**

For larger corpora (10K+), expect proportionally longer runtimes. Migration is designed to run unattended; you can leave it overnight. Network interruption during enumeration: re-run the same command — idempotency via `(fingerprint, source, created_at)` triple-match ensures no duplicates.

## Validate the migration

The migration script self-validates with a 10-sample byte-equality check via `engram fetch <id>` (NOT semantic search; semantic search is non-deterministic and would produce flaky validation). The report includes the sample results.

After the migration completes, do these manual checks:

### 1. Inspect the report

```bash
cat ~/.local/share/engram/personal/migration-report.json | head -80
```

Look for:
- `totals.errors == 0`
- `validation.passed == validation.sample_size` (typically 10/10)
- `by_prefix` distribution looks reasonable (no surprise spike of `Note` fallbacks)

### 2. Spot-check a few markdown files

```bash
ls ~/.local/share/engram/personal/thoughts/lesson/ | head -5
cat ~/.local/share/engram/personal/thoughts/lesson/<random-file>.md
```

The frontmatter should be well-formed YAML with all required fields (`id`, `prefix`, `portability`, `source`, `created_at`, `updated_at`, `fingerprint`). The `legacy_id` field carries the original Open Brain UUID. The body should be the original thought content, untouched except for line-ending normalization.

### 3. Run engram doctor

```bash
engram doctor
```

Expected: every check `[OK]`. The `pending_embedding_count` row should be 0 (every thought got an embedding). If non-zero, run `engram doctor --repair` to backfill.

### 4. MCP smoke test

Wire engram into your MCP client (Claude Code, Claude Desktop, etc) — see `docs/QUICKSTART.md` Step 4. Then run a search query that you know previously worked against Open Brain:

> "Search my thoughts for things I've learned about <topic-with-known-results>."

Compare the top-3 to what Open Brain would have returned. They don't have to be in identical order (different embedding model than Open Brain's OpenRouter-backed approach), but the relevant thoughts should be present.

### 5. 14-day trial

Use engram as your primary memory backend for **14 consecutive days**. During this window:

- Configure your MCP clients to point at engram, not Open Brain.
- Capture new thoughts via engram only.
- Open Brain stays running but receives no new captures.
- Search queries hit engram.

If at any point during the trial you find missing data, search-quality regressions, or operational issues, see "Rollback" below.

## Decommission Open Brain

**Only after the 14-day trial passes** (no missing-data complaints, no operational issues):

### 1. Final read-only snapshot

Take ONE more Supabase backup. This is the "in case of future regrets" snapshot.

### 2. Stop the Edge Function

```bash
supabase functions delete open-brain-mcp
```

### 3. Archive the database table

In the SQL Editor:

```sql
ALTER TABLE public.thoughts RENAME TO thoughts_archived_2026_05_DD;
```

Replace `2026_05_DD` with today's date. The data is preserved but no client can write to it.

### 4. Update your client configs

Anywhere you had Open Brain MCP references (`~/.claude/mcp_servers.json`, project CLAUDE.md files, dotfiles), replace them with the engram MCP server config. See `docs/QUICKSTART.md` Step 4.

### 5. Clean up secrets

- Revoke the Open Brain MCP access keys (Supabase dashboard → API → Service Role Key rotation).
- Remove the OpenRouter API key from your `.env` / secrets manager — engram does embeddings locally; you no longer need OpenRouter for memory.
- Update `~/.config/devkit/references.json` to remove the `open_brain_mcp_url` field (or comment it out).

After step 5, Open Brain is decommissioned. The Supabase project itself can stay (no cost on the free tier) or be deleted entirely.

## Rollback

If migration validation fails OR the 14-day trial reveals serious issues, here is the safe rollback procedure. **Do these steps in order; do NOT skip step 2.**

### 1. Stop engram

```bash
pkill engram      # or stop the MCP server gracefully via your MCP client
```

### 2. Preserve any post-migration captures BEFORE any destructive step

During the 14-day trial, you have been capturing into engram. Real captures may exist in the engram vault that are NOT in your original Open Brain export. Bundle them:

```bash
engram export \
  --vault primary \
  --output ~/post-migration-captures.tar.gz \
  --portability portable
```

(Engram doesn't currently filter exports by `--since` timestamp; the bundle includes all `portable` thoughts, which is a superset of trial-period captures. If you only want trial-period thoughts, manually inspect the bundle's `manifest.json` and the included markdown files' `created_at` fields.)

### 3. Revert client configs

Switch your MCP clients back to pointing at Open Brain. Roll back any CLAUDE.md / dotfiles changes that pointed clients at engram.

### 4. Re-enable Open Brain

If you started the decommission steps, re-enable the Edge Function:

```bash
supabase functions deploy open-brain-mcp
```

(If you only got as far as the 14-day trial without starting decommission, Open Brain is still running and this step is a no-op.)

### 5. Investigate

Review:
- `migration-report.json` for transform errors.
- The engram log file (`~/.local/share/engram/<vault>/engram.log` if logging is configured).
- The `post-migration-captures.tar.gz` summary for trial-period work.
- Any specific thought-not-found complaints from your trial.

### 6. Optionally delete the partial vault

ONLY after step 2 is confirmed:

```bash
rm -rf ~/.local/share/engram/personal/thoughts/
rm -rf ~/.local/share/engram/personal/.indexes/
```

The trial-period bundle from step 2 is on disk; the captures are not lost.

### 7. Re-import preserved trial captures into Open Brain (manual)

If the trial captures were valuable, manually replay them via Open Brain's `capture_thought` MCP tool — one call per thought from the bundle. This step is manual because automated re-import requires judgment about which thoughts deserve to be replayed; some may have been experimental.

### 8. Re-run migration after fixes

When the underlying issue is fixed, re-run the migration with `--append`. Idempotency (per the `(fingerprint, source, created_at)` triple-match rule) means previously-migrated thoughts are skipped and only failed ones are retried.

## Edge cases

| Symptom | What it means | What to do |
|---|---|---|
| Migration aborts with "embedding model failed to load" | FastEmbed couldn't initialize | Run `engram doctor --download-model` separately, then retry. |
| Migration aborts with "sqlite-vec extension fails to load" | Your Python's stdlib `sqlite3` was built without loadable extensions | Use uv-managed Python: `uv python install 3.11 && uv tool install engram-mcp`. |
| `migration-report.json` shows `errors[]` with "embedding generation failed" for some thoughts | Transient FastEmbed error | Re-run migration with `--append`; the second pass retries failed thoughts. |
| `fallback_assignments.prefix_Note_default` is large | Many Open Brain thoughts had no `[Prefix]` token | Open Brain didn't enforce prefix discipline; engram defaults to `Note` for these. You can manually re-categorize markdown files post-migration. |
| Your Open Brain has thoughts with image / file attachments stored in Supabase Storage | Phase 1 migration is text-and-metadata only; URLs to Supabase-hosted assets are migrated AS-IS | The migration report flags any thought whose body contains URLs to your Open Brain Supabase domain. Decide whether to archive assets manually before decommissioning. After decommissioning, those URLs will 404 unless preserved separately. |
| Network interruption during a long migration | Pagination state is lost | Re-run with `--append`. Idempotency ensures no duplicates. |
| Open Brain has thoughts with `created_at` in the future | Bad source-system data | Migration uses `now()` as `created_at`, logs a warning, preserves the original in the `legacy_created_at` frontmatter field. |
| The migration takes much longer than expected | Likely CPU embedding bottleneck on a large corpus | Let it run; FastEmbed on CPU averages ~1-2s per thought. For a 10K corpus, expect 3-5 hours. The migration is unattended-safe; leave it overnight. |

## Key differences from Open Brain you'll notice

After migration, when using engram day-to-day, expect these differences:

| Aspect | Open Brain | engram |
|---|---|---|
| Where data lives | Supabase Postgres | Markdown files on your disk |
| How to browse | Supabase Studio | Any text editor; Obsidian works on top |
| Sync across machines | Cloud-hosted (automatic) | Git push / pull to a remote you control |
| Embedding cost | OpenRouter API per capture | Local CPU; zero ongoing cost |
| Search latency | Network round-trip to Supabase | Local; sub-100ms p95 for ~10K thoughts |
| Offline access | None (network-required) | Full (local-first) |
| Recovery if vendor disappears | Restore from your snapshot | The markdown files ARE your data; nothing to recover |

## See also

- `docs/QUICKSTART.md` — 5-minute install + first capture flow.
- `docs/USE_CASES.md` — five concrete personas with example flows.
- `docs/ARCHITECTURE.md` — engram's components, data flow, storage layout.
- `docs/MULTI_MACHINE_SETUP.md` — once you've migrated, sync your vault across personal devices via git.
- The original Open Brain → engram migration spec lives at `~/repos/github.com/kpachhai/idea-forge/docs/superpowers/specs/2026-05-04-engram/04-MIGRATION.md` in the maintainer's planning repo (historical authority on the design).
