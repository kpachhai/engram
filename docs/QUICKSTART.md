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

## Step 3: Run a health check

```bash
engram doctor
```

Expected: a list of green check rows. If anything is RED, the message tells you how to fix it.

## Step 4: Wire engram into your MCP client

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

## Step 5: Capture your first thought

From inside an MCP client, ask the AI to capture something:

> "Capture a thought: [Lesson] Always run `engram doctor` after install — saves time when something's wrong."

The AI calls `capture_thought`. Engram writes a markdown file under `thoughts/lesson/`, indexes the embedding locally, and returns the ID.

Or capture from the command line:

```bash
echo "[Lesson] First capture from the CLI." | engram capture-stdin  # (if you wire one)
```

(Most users capture via the MCP tool — the AI is the natural capture surface.)

## Step 6: Search your memory

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

```bash
engram migrate-from-open-brain \
  --url https://your-ob.supabase.co/functions/v1 \
  --key <YOUR_OB_MCP_KEY> \
  --output-vault ~/.local/share/engram/personal \
  --confirm-supabase-snapshot-taken
```

The migrator paginates through every thought, generates UUID-v7 ids, computes fingerprints, and writes one markdown file per thought to the vault. A `migration-report.json` lands at the vault root. Migration is idempotent — re-running matches existing thoughts on `(fingerprint, source, created_at)`.
