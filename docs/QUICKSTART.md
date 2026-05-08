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
pip install engram-mcp
```

Or with uv:

```bash
uv tool install engram-mcp
```

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

- `default_user` — stamped as `source` on captures that don't carry an explicit one. Falls back to `~/.config/devkit/identity.json` `github_username` if unset, but explicit-in-the-config is more discoverable. Use your GitHub handle or any identifier you'll recognize later.
- `vaults` — list of vaults this machine has access to. The path can be absolute or `~`-prefixed; `engram` expands it. The `name` is a stable handle used in CLI commands and frontmatter; the `role` is one of `primary` / `read-only` / `team-write`.
- `log_level` / `log_format` — operational defaults; tweak as needed.

Multi-vault setups (friend-imported corpus, team-shared vault) add additional rows here. See `docs/MULTI_VAULT_SETUP.md`.

## Step 4: Run a health check

```bash
engram doctor
```

Expected: a list of green check rows. If anything is RED, the message tells you how to fix it. Common first-time issues:

- **`config_missing`** — you skipped Step 3.
- **`vault_path_missing`** — the `path` in `config.yaml` doesn't point at the vault `engram init` created.
- **`embedding_model_missing`** — the FastEmbed model isn't downloaded yet. Run `engram doctor --download-model` (one-shot, ~130 MB).

## Step 5: Wire engram into your MCP client

### Claude Code

Add engram to your MCP server config (`~/.claude/mcp_servers.json` or via the Claude Code settings UI):

```json
{
  "mcpServers": {
    "engram": {
      "command": "engram",
      "args": ["serve"]
    }
  }
}
```

Restart Claude Code. You should see seven engram tools registered: `capture_thought`, `search_thoughts`, `list_thoughts`, `thought_stats`, `fetch`, `summarize_thought`, `synthesize_thoughts`.

### Claude Desktop

Same as Claude Code but the config lives at `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the OS equivalent.

### Other MCP clients

Engram speaks stdio MCP. Any client that can launch a subprocess and speak the MCP protocol over stdin/stdout will work. Point it at `engram serve`.

## Step 6: Capture your first thought

From inside an MCP client, ask the AI to capture something:

> "Capture a thought: [Lesson] Always run `engram doctor` after install — saves time when something's wrong."

The AI calls `capture_thought`. Engram writes a markdown file under `thoughts/lesson/`, indexes the embedding locally, and returns the ID.

Or capture from the command line:

```bash
echo "[Lesson] First capture from the CLI." | engram capture-stdin  # (if you wire one)
```

(Most users capture via the MCP tool — the AI is the natural capture surface.)

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

## Troubleshooting

### `engram doctor` shows `git_version_floor` FAIL

You need git 2.30+. Run `brew install git` or your platform equivalent.

### `engram serve` says "sqlite-vec extension not loaded"

Your Python's stdlib `sqlite3` was built without `--enable-loadable-sqlite-extensions`. The fix is to use a Python that supports loadable extensions; the easiest path is to install via [uv](https://docs.astral.sh/uv/):

```bash
uv python install 3.11
uv tool install engram-mcp
```

### Search returns no results even though I captured thoughts

Check `engram doctor` for `embedding_status_pending` rows — it means the embedding model failed to load on capture. Run `engram doctor --repair` to backfill, or `engram reindex` to rebuild from markdown.

### My MCP client doesn't see the engram tools

Restart the client after adding the server config. Run `engram serve` directly in a terminal to confirm the binary works; if it prints "MCP server started" and waits for input, the server side is fine — the issue is in the client config.

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
