# engram

> Personal AI memory backend - portable, sovereign, protocol-compatible.

`engram` is an MCP server that gives AI assistants (Claude Code, Claude Desktop, any MCP-aware client) a persistent memory store you fully own. Thoughts live as plain markdown files; vector search runs locally; sync is just `git push`.

## Why engram

Three problems with hosted AI memory tools, and engram's answer:

| Problem | Hosted AI memory tools | engram |
|---|---|---|
| Vendor lock-in | Years of context on someone else's database | Markdown files you can read in any editor |
| Privacy boundaries | One hosted store can't model personal-vs-employer-confidential | Physically separate vaults; the work vault data isn't on the personal disk |
| Cross-machine fragmentation | Service down or unreachable = no memory | Git pull/push = sync; works offline |
| API stability | Vendor changes break your prompts | MCP-native, drop-in compatible with Open Brain's tool surface |
| Compliance review | "Where does the data live?" → "the cloud" | "Where does the data live?" → "this directory" |

If you run an AI assistant daily and want its memory to (a) survive across sessions and machines, (b) honor the personal/work boundary, and (c) never get held hostage by a vendor — engram is built for you.

## Quickstart

```bash
# 1. Install (Python 3.11+; macOS or Linux)
pip install engram-mcp

# 2. Scaffold a vault
engram init ~/.local/share/engram/personal

# 3. Tell engram where to find the vault
mkdir -p ~/.config/engram && cat > ~/.config/engram/config.yaml <<'EOF'
default_user: <your-username>
vaults:
  - name: personal
    path: ~/.local/share/engram/personal
    role: primary
EOF

# 4. Health check
engram doctor

# 5. Wire into Claude Code (or any MCP client)
# Add to ~/.claude/mcp_servers.json:
# {
#   "mcpServers": {
#     "engram": { "command": "engram", "args": ["serve"] }
#   }
# }
```

Now ask your AI assistant: *"Capture a thought: [Lesson] engram took five minutes to set up."*

That's it. Search it back any time:

> "Search my thoughts for things I've learned about engram."

Full walkthrough: **[docs/QUICKSTART.md](docs/QUICKSTART.md)**.

## Use cases

| Persona | Solves |
|---|---|
| **Solo knowledge worker** | AI conversations contain real learning; without engram, none of it survives the session |
| **Multi-machine personal user** | Captures from desktop are searchable from laptop after the next git pull |
| **Privacy-bounded worker** | Personal vault and work vault on physically separate machines; no IT compliance worry |
| **Trust-network sharer** | Export a portable bundle to a peer; they import it as a read-only friend-vault |
| **Small team tech lead** | Shared team vault with GPG-attributed captures + push-time policy enforcement |

Concrete examples for each: **[docs/USE_CASES.md](docs/USE_CASES.md)**.

## How does it compare?

| Tool | engram is better when... | Other tool is better when... |
|---|---|---|
| **Mem0** | You want markdown SoT, local-first, work-laptop-friendly | You're building autonomous agents that decide what to remember |
| **Letta (MemGPT)** | Personal corpora, single-tier search | Long-running agents with explicit tiered-memory needs |
| **basic-memory** | Apache-2.0 licensing, prefix taxonomy, multi-vault | AGPL-3.0 is fine and you want simpler scope |
| **Open Brain (OB1)** | You want off-SaaS + work-laptop access | You're already on it and don't want to migrate |
| **Obsidian + Smart Connections** | You need a headless backend (no GUI dep) | You want a polished GUI for browsing |
| **engraph** | Open Brain MCP API parity, prefix taxonomy | You want a single Rust binary |
| **Raw markdown + grep** | You've crossed ~100 thoughts and need semantic search | You have under 50 thoughts; grep is enough |

Detailed analysis: **[docs/COMPARISONS.md](docs/COMPARISONS.md)**.

## Tools exposed via MCP

Seven tools — five core (deterministic, no LLM calls), two optional LLM-mediated:

- `capture_thought(content, metadata?)` — write a new thought
- `search_thoughts(query, k?, filter?)` — semantic top-k
- `list_thoughts(limit?, offset?, filter?, sort?)` — filtered + sorted + paginated
- `thought_stats()` — aggregate counts
- `fetch(id)` — lookup by id
- `summarize_thought(id)` — LLM-mediated single-thought summary (opt-in; also exposed as `engram summarize <id>` CLI)
- `synthesize_thoughts(query, k, filter)` — LLM-mediated cross-vault RAG (opt-in; also exposed as `engram synthesize "<query>"` CLI)

The five-tool core surface is stable for the v1.x lifetime; LLM tools follow the same stability commitment.

## Architecture in one paragraph

Markdown files in `<vault>/thoughts/<prefix>/...` are the source of truth. SQLite + sqlite-vec at `<vault>/.indexes/` is a regenerable index. Embeddings (`BAAI/bge-small-en-v1.5` via FastEmbed) run locally on CPU. Sync uses the system `git` CLI — your personal vault is just a git repo. Multi-vault deployments mount one `primary` vault + any number of `read-only` mirrors and `team-write` shared vaults; the routing dispatcher decides where each capture lands. Team vaults add GPG-fingerprint-bound sender attribution + a stdlib-only Python `pre-receive` hook on the git remote. Optional LLM tools (`summarize_thought`, `synthesize_thoughts`) compose with strict per-thought portability gates.

Full walkthrough with diagrams: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Documentation

**Get started:**
- [QUICKSTART.md](docs/QUICKSTART.md) — five-minute install + first capture
- [USE_CASES.md](docs/USE_CASES.md) — five concrete personas with example flows
- [COMPARISONS.md](docs/COMPARISONS.md) — engram vs Mem0, Letta, basic-memory, Open Brain, Obsidian + Smart Connections, engraph

**Deeper:**
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, storage flow, sync state machine, two-layer security boundary
- [MULTI_MACHINE_SETUP.md](docs/MULTI_MACHINE_SETUP.md) — git-based sync across personal devices
- [MULTI_VAULT_SETUP.md](docs/MULTI_VAULT_SETUP.md) — role taxonomy + per-user config layout
- [FRIEND_SHARE_GUIDE.md](docs/FRIEND_SHARE_GUIDE.md) — bundle export/import flow
- [TEAM_BRAIN_GUIDE.md](docs/TEAM_BRAIN_GUIDE.md) — shared team vault setup + policy + revocation
- [LLM_FEATURES.md](docs/LLM_FEATURES.md) — optional LLM-mediated tools

**Design rationale:**
- [docs/adr/](docs/adr/) — Architecture Decision Records (one per major design choice)

## Local install (without PyPI)

You can run engram entirely from a working copy of this repo:

```bash
git clone https://github.com/kpachhai/engram
cd engram
uv sync --all-extras --dev

# Run the CLI directly
uv run engram init ~/.local/share/engram/personal
mkdir -p ~/.config/engram && cat > ~/.config/engram/config.yaml <<'EOF'
default_user: <your-username>
vaults:
  - name: personal
    path: ~/.local/share/engram/personal
    role: primary
EOF
uv run engram doctor --download-model
uv run engram doctor
uv run engram serve

# Build a wheel + install in a clean venv to validate packaging
uv build
python -m venv /tmp/engram-test-venv
/tmp/engram-test-venv/bin/pip install dist/engram_mcp-*.whl
/tmp/engram-test-venv/bin/engram --version
```

## Development

```bash
git clone https://github.com/kpachhai/engram
cd engram
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run mypy
```

The test suite (1100+ tests) covers unit, integration, and hermetic CLI smoke against the installed binary.

## Migrating from Open Brain

Existing Open Brain (OB1) corpus migrates in one command:

```bash
engram migrate-from-open-brain \
  --url https://your-ob.supabase.co/functions/v1 \
  --key <YOUR_OB_MCP_KEY> \
  --output-vault ~/.local/share/engram/personal \
  --confirm-supabase-snapshot-taken
```

Idempotent (re-run safely; matches existing thoughts on `(fingerprint, source, created_at)`). Generates `migration-report.json` with counts + 10-thought round-trip sample.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The project repo holds code only; user vaults live separately and never get pushed here.

## License

[Apache-2.0](LICENSE).
