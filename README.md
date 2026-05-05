# engram

> Personal AI memory backend - portable, sovereign, protocol-compatible.

`engram` is an MCP server that gives AI assistants (Claude Code, Claude Desktop, any MCP-aware client) a persistent memory store you fully own. Thoughts live as plain markdown files; vector search runs locally; sync is just `git push`.

## Status

Pre-1.0. Phases 1-4 are code-complete:

* Phase 1 (solo MVP + Open Brain migration)
* Phase 2 (multi-machine personal sync via git transport)
* Phase 3 (multi-vault foundation + friend-share + optional LLM)
* Phase 4 (Team Brain - multi-target write + GPG-bound sender
  attribution + per-prefix routing + server-side `pre-receive` hook)

See `docs/PHASE_<N>_CODE_COMPLETE.md` for each phase's exit-criteria
validation.

Phase 4 setup + flow guides:

* `docs/TEAM_BRAIN_GUIDE.md` - bootstrapping a team vault, member
  enrollment, hook install per platform, revocation, disaster
  recovery.
* `docs/MULTI_VAULT_SETUP.md` - role taxonomy table covering all
  three roles (`primary`, `read-only`, `team-write`).
* `docs/adr/007-team-brain.md` - Phase 4 design decisions D1-D9.

Phase 3 setup + flow guides remain valid:

* `docs/FRIEND_SHARE_GUIDE.md` - export / import bundle workflow.
* `docs/LLM_FEATURES.md` - optional LLM-mediated tools
  (`summarize_thought` + `synthesize_thoughts`) with per-thought
  portability gates and daily cost cap.
* `docs/adr/006-multi-vault-and-llm.md` - Phase 3 design rationale.

Multi-machine setup (Phase 2 baseline):
`docs/MULTI_MACHINE_SETUP.md`. The pinned spec lives at
`docs/superpowers/specs/2026-05-04-engram/` in the maintainer's
planning repo.

### Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Solo MVP + Open Brain migration | code-complete |
| 2 | Multi-machine sync via git transport | code-complete |
| 3 | Multi-vault + friend-share via bundles + optional LLM | code-complete |
| 4 | Team Brain (multi-writer + GPG attribution + server hook) | code-complete |
| 5-6 | Org / enterprise | gated by adoption |

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

## Local install (without PyPI)

You can run engram entirely from a working copy of this repo - no PyPI required. Useful for development, pre-publish smoke tests, and air-gapped operation.

```bash
# 1. Clone + install dev deps via uv
git clone https://github.com/kpachhai/engram
cd engram
uv sync --all-extras --dev

# 2. Run the CLI directly (uv resolves the console script)
uv run engram init ~/my-vault
uv run engram doctor --download-model
uv run engram doctor
uv run engram serve --config ~/my-vault/engram.config.yaml

# 3. (Optional) Build a wheel and install in a clean venv to validate packaging
uv build                                                   # writes dist/engram_mcp-<version>-py3-none-any.whl
python -m venv /tmp/engram-test-venv
/tmp/engram-test-venv/bin/pip install dist/engram_mcp-*.whl
/tmp/engram-test-venv/bin/engram --version
/tmp/engram-test-venv/bin/engram doctor
```

This path validates every code-side exit criterion in `docs/PHASE_1_CODE_COMPLETE.md` and `docs/PHASE_2_CODE_COMPLETE.md` without publishing. The two-machine convergence test runs against a local bare repo - see the **"Local two-machine smoke test"** section of `docs/MULTI_MACHINE_SETUP.md`.

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
| 1 | Solo MVP + OB migration | Code-complete (see `docs/PHASE_1_CODE_COMPLETE.md`); 6 operational criteria pending live deployment |
| 2 | Multi-machine personal sync (git) | Code-complete (see `docs/PHASE_2_CODE_COMPLETE.md`); 1 operational criterion (7-day two-machine dogfood) pending |
| 3 | Multi-vault foundation + friend-share + optional LLM features | Code-complete (see `docs/PHASE_3_CODE_COMPLETE.md`); 1 operational criterion (7-day mixed-corpus dogfood) pending |
| 4 | Team Brain (multi-target write + GPG-bound attribution + per-prefix routing + server-side hook) | Code-complete (see `docs/PHASE_4_CODE_COMPLETE.md`); 2 operational criteria (3-machine 7-day dogfood + revocation ceremony) pending |
| 5-6 | Organization / enterprise | Designed, gated by adoption |

Decision gates between phases are explicit in `03-ROADMAP.md`. Phase 5+ is gated by real-org adoption, not speculation.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The project repo holds code only; user vaults live separately and never get pushed here.

## License

[Apache-2.0](LICENSE).
