# engram - Quickstart

Five minutes from "no engram" to "AI assistant has memory."

## Prerequisites

- Python 3.11 or later
- macOS or Linux (Windows via WSL works)
- Either pip or [uv](https://docs.astral.sh/uv/) (uv recommended)
- An MCP-aware client: Claude Code, Claude Desktop, or any other MCP-compatible AI tool

Optional, only needed for specific features:

- `git` (for multi-machine sync and friend-share — already installed almost everywhere)
- `gpg` (for team-vault sender attribution)

## Step 1: Install

```bash
pip install engram-mcp-server
```

Or with uv:

```bash
uv tool install engram-mcp-server
```

Or from a source clone (editable - tracks your local `git pull`s, and is the path most contributors use):

```bash
cd ~/repos/github.com/<your-username>/engram
uv tool install --editable .
```

All three forms drop the `engram` binary at `~/.local/bin/engram`, which is what Claude Code's MCP launcher needs (it does not source your shell rc, so a bare `engram` on PATH is not enough - we register the absolute path in Step 5).

Verify the install:

```bash
engram --version
```

## Step 2: Create your first vault

A "vault" is just a directory containing your thoughts as markdown files plus a SQLite index. Pick a path you want your memory to live at:

```bash
engram init ~/.local/share/engram/personal
```

This creates:

```
~/.local/share/engram/personal/
├── thoughts/                  # one subdirectory per prefix
│   ├── lesson/
│   ├── decision/
│   └── ...
├── .indexes/                  # SQLite + sqlite-vec index (regenerable)
├── engram.config.yaml         # vault config
├── .gitignore                 # so .indexes/ never gets committed
└── README.md                  # stub
```

## Step 3: Tell engram where to find the vault

`engram init` scaffolds the vault directory + writes a per-vault `engram.config.yaml` INSIDE it. But `engram doctor` and `engram serve` need a separate USER-level config at `~/.config/engram/config.yaml` to know which vault(s) to mount on this machine. Two different files; both are required.

Create the user-level config:

```bash
mkdir -p ~/.config/engram
cat > ~/.config/engram/config.yaml <<'EOF'
default_user: <your-username>
vaults:
  - name: personal
    path: ~/.local/share/engram/personal
    role: primary
log_level: INFO
log_format: text
EOF
```

What each field does:

- `default_user` — stamped as `source` on captures that don't carry an explicit one. Use your GitHub handle or any identifier you'll recognize later. If you leave it out, engram reads `github_username` from the optional `~/.config/devkit/identity.json` when that file exists, and otherwise uses `$USER`; setting it here is more discoverable than either.
- `vaults` — list of vaults this machine has access to. The path can be absolute or `~`-prefixed; `engram` expands it. The `name` is a stable handle used in CLI commands and frontmatter; the `role` is one of `primary` / `read-only` / `team-write`.
- `log_level` / `log_format` — operational defaults; tweak as needed.

Multi-vault setups (friend-imported corpus, team-shared vault) add additional rows here. See `docs/MULTI_VAULT_SETUP.md`.

## Step 4: Run a health check

```bash
engram doctor
```

Every row carries one of four statuses, and the run ends with a count line and a
verdict line. An excerpt from a real run against a local-only vault (rows elided):

```
  [OK  ] index_consistency: 0 thoughts indexed; matches on-disk count
  [SKIP] consolidate_journal_orphan: skipped (no consolidation state)
  [WARN] llm_provider_reachable: provider llama_cpp did not respond to health_check; LLM tools may fail until the provider is reachable.

engram doctor: 37 checks - 12 passed, 22 skipped, 3 warnings, 0 failures
engram doctor: warnings (operational, with caveats)
```

The row count and the bucket sizes depend on how the vault is set up; what matters is
that the count line always prints all four buckets, zeros included.

- `[OK  ]` - the check ran and the vault is healthy on that point.
- `[SKIP]` - the check did not run, because what it inspects is not present. A vault
  that is not a git repository skips thirteen of the fourteen sync checks (the
  conflict-marker scan runs either way); a vault that has never run `engram consolidate`
  skips the two consolidation rows. Nothing is wrong, but the row answered nothing
  either, so it is counted apart from the passes.
- `[WARN]` - degraded but serviceable. The row text names the remediation.
- `[FAIL]` - resolve before serving.

Exit codes: `0` when nothing is degraded (skips included), `1` when the worst row is a
WARN, `2` when anything FAILed. A run that exits 0 with skipped rows says so on the
verdict line ("no warnings or failures, but N of M checks did not run") rather than
claiming everything is green.

If anything is `[FAIL]`, the message text on that row tells you what to fix. Common
first-time issues:

- A FAIL row on `thoughts_dir` or `index_dir` means the `path` in `~/.config/engram/config.yaml` doesn't point at the vault `engram init` created. Re-check Step 3.
- A FAIL row on `sqlite_vec` means your Python's stdlib `sqlite3` was built without loadable extensions. See the Troubleshooting section below for the uv-based fix.
- A WARN row on `pending_embeddings` means the FastEmbed model wasn't ready when some thoughts were captured. Run `engram doctor --download-model` (one-shot, ~130 MB) then `engram doctor --repair`.

## Step 5: Wire engram into your MCP client

### Claude Code

Register engram as a user-scope MCP server (available across all projects):

```bash
claude mcp add --scope user engram -- "$(which engram)" serve
```

Confirm with `claude mcp list` - you should see an `engram:` line. Under the hood this writes to `~/.claude.json`; don't hand-edit that file - use the `claude mcp` CLI to add or remove servers. Passing the absolute path (`"$(which engram)"`) rather than the bare command matters because Claude Code launches MCP subprocesses with a stripped PATH that does not source your shell rc.

Restart Claude Code. On the single-vault setup above you should see six engram tools registered: `capture_thought`, `search_thoughts`, `list_thoughts`, `thought_stats`, `fetch`, `delete_thought`. The two optional LLM tools (`summarize_thought`, `synthesize_thoughts`) are wired only once a second vault is mounted - see [MULTI_VAULT_SETUP.md](MULTI_VAULT_SETUP.md) and [LLM_FEATURES.md](LLM_FEATURES.md).

The first session has a small (~1-2s) spawn latency while engram boots its per-vault daemon and loads the embedding model. Subsequent concurrent Claude Code sessions on the same vault attach in milliseconds — you can run two or more sessions against the same vault simultaneously. See [DAEMON_MODE.md](DAEMON_MODE.md) for the operator-facing controls (`engram daemon status`, `engram daemon stop`, etc).

### Claude Desktop

Same as Claude Code but the config lives at `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the OS equivalent.

### Other MCP clients

Engram speaks stdio MCP. Any client that can launch a subprocess and speak the MCP protocol over stdin/stdout will work. Point it at `engram serve`.

## Step 6: Capture your first thought

From inside an MCP client, ask the AI to capture something:

> "Capture a thought: [Lesson] Always run `engram doctor` after install — saves time when something's wrong."

The AI calls `capture_thought`. Engram writes a markdown file under `thoughts/lesson/`, indexes the embedding locally, and returns the ID.

Most users capture via the MCP tool — the AI is the natural capture surface. There is intentionally no `engram capture` CLI subcommand; if you need to seed thoughts from scripts, drop markdown files directly into `thoughts/<prefix>/` and run `engram reindex`.

## Step 7: Search your memory

> "Search my thoughts for things I've learned about installing engram."

The AI calls `search_thoughts` with your query. Engram embeds the query locally, runs ANN search via sqlite-vec, and returns the top results.

## What just happened

- Markdown files are the source of truth — open them in any editor, sync them via git, browse them in Obsidian.
- The index in `.indexes/` is regenerable — `engram reindex` rebuilds it from the markdown if it ever gets corrupted.
- Embeddings are local — `BAAI/bge-small-en-v1.5` runs on CPU; no API calls.
- Capture and search hit your disk only — no telemetry, no analytics, no network.

## Next steps

- **Sync across multiple personal machines:** [docs/MULTI_MACHINE_SETUP.md](MULTI_MACHINE_SETUP.md)
- **Share a curated bundle with a friend:** [docs/FRIEND_SHARE_GUIDE.md](FRIEND_SHARE_GUIDE.md)
- **Mount multiple vaults at once:** [docs/MULTI_VAULT_SETUP.md](MULTI_VAULT_SETUP.md)
- **Run a shared team brain:** [docs/TEAM_BRAIN_GUIDE.md](TEAM_BRAIN_GUIDE.md)
- **Use optional LLM-mediated tools (summarize / synthesize):** [docs/LLM_FEATURES.md](LLM_FEATURES.md)
- **Run N concurrent Claude Code sessions on one vault (daemon mode):** [docs/DAEMON_MODE.md](DAEMON_MODE.md)

## Troubleshooting

### `engram doctor` shows `git_version_floor` FAIL

You need git 2.30+. Run `brew install git` or your platform equivalent.

### `engram serve` says "sqlite-vec extension not loaded"

Your Python's stdlib `sqlite3` was built without `--enable-loadable-sqlite-extensions`. The fix is to use a Python that supports loadable extensions; the easiest path is to install via [uv](https://docs.astral.sh/uv/):

```bash
uv python install 3.11
uv tool install engram-mcp-server
```

### Search returns no results even though I captured thoughts

Check `engram doctor` for a `pending_embeddings` WARN row — it means the embedding model failed to load on capture. Run `engram doctor --repair` to backfill, or `engram reindex` to rebuild from markdown.

### `model integrity check failed ... Refusing to load`

A cached model file no longer matches the SHA-256 pinned in
`src/engram/embedding/model_hashes.py`, so engram refuses to embed through it rather
than loading it anyway. A truncated or interrupted download is the usual cause. Note
that `engram doctor`'s `embedding_cache_integrity` row is a presence check, not a hash
check, so it can report the snapshot intact while the load still refuses. Either way
that row prints the snapshot directory: delete it and re-run
`engram doctor --download-model` to re-fetch.

### My MCP client doesn't see the engram tools

Restart the client after adding the server config. Run `engram daemon status` to check whether the per-vault daemon is healthy — text output should show `pid`, `uptime`, and the socket path. If it says `not running`, that's fine; the daemon spawns lazily on the first `engram serve` invocation. To debug the daemon itself, run `engram daemon start` in a terminal and inspect its log (`engram daemon logs --follow`).

## Migrating from Open Brain

If you have an existing Open Brain (OB1) deployment on Supabase:

**Important:** the built-in `engram migrate-from-open-brain` MCP-based command does NOT work against any reasonably-recent OB1 deployment. OB1's MCP tools (`list_thoughts`, `fetch`, etc.) return human-readable text content, not structured records that engram can import — there's no per-thought id field to enumerate. The recommended path today is direct Postgres access against OB1's Supabase `thoughts` table.

See `docs/OPENBRAIN_MIGRATION_GUIDE.md` for the full walkthrough including the reference Postgres-direct migration script. Brief shape:

```bash
# 1. Get the Postgres connection string from Supabase Dashboard → Project Settings → Database.
export OB1_POSTGRES_URL='postgresql://postgres.<ref>:<pass>@<host>:<port>/postgres'

# 2. Run the reference script from the engram source repo so engram is importable.
cd ~/repos/github.com/<your-username>/engram
uv run --with 'psycopg[binary]' python <path/to/script>/migrate_thoughts_to_engram.py --vault personal --dry-run
```

The script reuses engram's existing `_migrate_one` pipeline (prefix parsing, fingerprint, atomic write, FastEmbed embedding). It's idempotent — re-running matches existing thoughts on `(fingerprint, source, created_at)` so a partial failure is safe to re-run.
