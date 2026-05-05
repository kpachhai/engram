# engram

> Personal AI memory backend - portable, sovereign, protocol-compatible.

`engram` is an MCP server that gives AI assistants (Claude Code, Claude Desktop, any MCP-aware client) a persistent memory store you fully own. Thoughts live as plain markdown files; vector search runs locally; sync is just `git push`.

## Status

Pre-1.0. Phase 1 (solo MVP + Open Brain migration) is in active development. The pinned spec lives at `docs/superpowers/specs/2026-05-04-engram/` in the maintainer's planning repo.

## Why

Three problems with existing AI memory tools:

1. **Vendor lock-in.** Hosted SaaS holds your years of context.
2. **Privacy boundary failures.** A single hosted store cannot model the personal-vs-employer-confidential boundary.
3. **Cross-machine fragmentation.** No graceful degradation when the hosted service is unreachable (work laptop, offline, downtime).

`engram`'s answer: markdown source-of-truth, git as transport, MCP as the universal API.

## Quickstart

```bash
# 1. Install (Python 3.11+)
pip install engram-mcp

# 2. Scaffold a fresh vault (or point at an existing one)
engram init ~/my-vault

# 3. Configure
mkdir -p ~/.config/engram
cat > ~/.config/engram/config.yaml <<EOF
default_user: <your-handle>
vaults:
  - name: personal
    path: ~/my-vault
    role: primary
EOF

# 4. Pre-download the embedding model
engram doctor --download-model

# 5. Verify
engram doctor

# 6. Wire into Claude Code (~/.config/claude-code/mcp.json):
# {
#   "mcpServers": {
#     "engram": { "command": "engram", "args": ["serve"] }
#   }
# }
```

Detailed install + multi-machine + air-gapped install steps live in `docs/operations.md` (coming soon; see `07-OPERATIONS.md` in the spec for now).

## Tools exposed via MCP

Five tools, intentionally matching the Open Brain MCP surface so existing prompts and skills work unchanged:

- `capture_thought(content, metadata?)`
- `search_thoughts(query, k?, filter?)`
- `list_thoughts(limit?, offset?, filter?, sort?)`
- `thought_stats()`
- `fetch(id)`

The five-tool surface is stable for the v1.x lifetime per the API stability commitment in `02-TECHNICAL_DESIGN.md`.

## Architecture

- **Storage:** markdown files in `<vault>/thoughts/<prefix>/...`, SQLite + sqlite-vec at `<vault>/.indexes/`.
- **Embeddings:** `BAAI/bge-small-en-v1.5` via FastEmbed, local-only, ~130MB model.
- **Sync:** system `git` CLI (Phase 2+). Personal and work vaults live in physically separate repos.
- **Server:** FastMCP, stdio-only.
- **Config:** layered (defaults → user → vault → env → CLI flags) via pydantic-settings.

See [`docs/adr/`](docs/adr/) for architectural decision records.

## Development

```bash
git clone https://github.com/kpachhai/engram
cd engram
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run mypy
```

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Solo MVP + OB migration | In progress |
| 2 | Multi-machine personal sync (git) | Designed, not built |
| 3 | Multi-vault foundation + friend-share + optional LLM features | Designed, not built |
| 4-6 | Team / organization / enterprise | Designed, gated by adoption |

Decision gates between phases are explicit in `03-ROADMAP.md`. Phase 4+ is gated by real-team adoption, not speculation.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The project repo holds code only; user vaults live separately and never get pushed here.

## License

[Apache-2.0](LICENSE).
