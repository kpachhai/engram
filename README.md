# engram

> Personal AI memory backend - portable, sovereign, protocol-compatible.

`engram` is an MCP server that gives AI assistants (Claude Code, Claude Desktop, any MCP-aware client) a persistent memory store you fully own. Thoughts live as plain markdown files; vector search runs locally; sync is just `git push`.

## Why engram

Memory is where AI lock-in actually happens. Models are stateless and interchangeable; the accumulated record of what your assistant has learned about you and your work is neither. The open-vs-closed harness debate landed on exactly this point in spring 2026: a harness that stores memory behind its API owns your context, and vendors have every incentive to keep it there ("[Your harness, your memory](https://x.com/hwchase17/status/2042978500567609738)", Harrison Chase, April 2026). Portable memory is the optionality that keeps every other choice - harness, model, vendor - reversible.

Six problems with hosted AI memory tools, and engram's answer:

| Problem | Hosted AI memory tools | engram |
|---|---|---|
| Vendor lock-in | Years of context on someone else's database; switching harnesses means abandoning it | Markdown files readable in any editor, by any harness, today |
| Privacy boundaries | One hosted store can't model personal-vs-employer-confidential | Physically separate vaults; the work vault data isn't on the personal disk |
| Cross-machine fragmentation | Service down or unreachable = no memory | Git pull/push = sync; works offline |
| API stability | Vendor changes break your prompts | MCP-native, drop-in compatible with Open Brain's tool surface |
| Compliance review | "Where does the data live?" → "the cloud" | "Where does the data live?" → "this directory" |
| Memory curation | Server-side, on the vendor's schedule, invisible to you | `engram consolidate` (v0.6.0+): local report-then-action curation; nothing merges without your review |

Growth is the quiet failure mode: semantic memory that only accretes interferes with itself - retrieval crowds and false recall rises as the store grows (the no-escape theorem; "[The Price of Meaning](https://x.com/ashwingop/status/2042604890988646750)", April 2026). The architecture that survives is three legs: an exact episodic record, a semantic index, and consolidation managing the boundary between them. engram ships all three - markdown files are the episodic record, the local vector index is the semantic layer, and `engram consolidate` (v0.6.0+) is the consolidation pass. On its first run against a real production vault it surfaced 16 near-duplicate clusters in about 4 seconds and merged none of them: apply is gated behind your review, and originals are archived, never deleted.

If you run an AI assistant daily and want its memory to (a) survive across sessions and machines, (b) honor the personal/work boundary, (c) stay yours through any harness or model switch, and (d) keep its signal as it grows - engram is built for you.

## Quickstart

> **Install `engram-mcp-server`, not `engram-mcp`.** The shorter name on PyPI
> belongs to an unrelated project. The package installs the `engram` command
> and the `engram` import package; only the install-time name differs.

```bash
# 1. Install (Python 3.11+; macOS or Linux)
pip install engram-mcp-server

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
claude mcp add --scope user engram -- "$(which engram)" serve
# Claude Desktop uses a different config path; see docs/QUICKSTART.md Step 5.
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

Eight tools — six core (deterministic, no LLM calls), two optional LLM-mediated:

- `capture_thought(content, metadata?)` — write a new thought
- `search_thoughts(query, k?, filter?)` — semantic top-k
- `list_thoughts(limit?, offset?, filter?, sort?)` — filtered + sorted + paginated
- `thought_stats()` — aggregate counts
- `fetch(id)` — lookup by id
- `delete_thought(id, confirm)` — delete a thought; AI clients MUST call once with `confirm=False` to obtain a preview, then again with `confirm=True` after explicit user approval (also exposed as `engram delete <id>` CLI with typed-string confirmation gate)
- `summarize_thought(id)` — LLM-mediated single-thought summary (opt-in; also exposed as `engram summarize <id>` CLI)
- `synthesize_thoughts(query, k, filter)` — LLM-mediated cross-vault RAG (opt-in; also exposed as `engram synthesize "<query>"` CLI)

The six-tool core surface is stable for the v1.x lifetime; LLM tools follow the same stability commitment.

## Architecture in one paragraph

Markdown files in `<vault>/thoughts/<prefix>/...` are the source of truth. SQLite + sqlite-vec at `<vault>/.indexes/` is a regenerable index. Embeddings (`BAAI/bge-small-en-v1.5` via FastEmbed) run locally on CPU. Sync uses the system `git` CLI — your personal vault is just a git repo. Multi-vault deployments mount one `primary` vault + any number of `read-only` mirrors and `team-write` shared vaults; the routing dispatcher decides where each capture lands. Team vaults add GPG-fingerprint-bound sender attribution + a stdlib-only Python `pre-receive` hook on the git remote. Optional LLM tools (`summarize_thought`, `synthesize_thoughts`) compose with strict per-thought portability gates. In v0.5.0+, `engram serve` runs as a thin proxy that auto-spawns a per-vault daemon over a Unix Domain Socket — N concurrent Claude Code sessions can attach to the same vault simultaneously.

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
- [CONSOLIDATION.md](docs/CONSOLIDATION.md) — report-then-action vault curation: dedup clusters, stale + contradiction candidates (v0.6.0+)
- [DAEMON_MODE.md](docs/DAEMON_MODE.md) — daemon mode: multi-session support, operator + migration guide (v0.5.0+)

**Design rationale:**
- [docs/adr/](docs/adr/) — Architecture Decision Records (one per major design choice)

## Local install (without PyPI)

You can run engram entirely from a working copy of this repo:

```bash
git clone https://github.com/kpachhai/engram  # pii-allow:repo-url
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
/tmp/engram-test-venv/bin/pip install dist/engram_mcp_server-*.whl
/tmp/engram-test-venv/bin/engram --version
```

## Updating engram

Pick the section that matches how you installed.

### Installed via PyPI

```bash
uv tool upgrade engram-mcp-server     # or: pip install --upgrade engram-mcp-server
engram doctor                  # surface any vault / schema drift
# Restart your MCP client (Claude Code, Claude Desktop, etc.)
```

### Installed editable from source (`uv tool install --editable .`)

Editable installs link the binary's venv back to your source clone via a `.pth` file, so Python-level source changes are picked up the next time `engram` runs — no reinstall needed. **But** new entries in `pyproject.toml` `[project.dependencies]` live in the tool's venv, not the source dir, so a dependency bump requires a reinstall.

The safe defensive workflow that covers both cases:

```bash
cd <your-engram-source-clone>
git pull origin main

# Idempotent if no dep changes; cheap insurance if there were.
uv tool install --editable . --reinstall

# Surface any schema / config drift.
engram doctor

# Restart your MCP client - new sessions pick up the new code.
```

You can skip the `--reinstall` step if `git diff HEAD@{1} HEAD -- pyproject.toml uv.lock` is empty (no dependency changes). Running it unconditionally is the "just run it" habit that avoids the "did pyproject.toml change? I forgot to check" failure mode.

### What about the running engram process?

In v0.5.0+, `engram serve` runs as a thin proxy that auto-spawns a
per-vault **daemon** on first use. The daemon is a long-lived process
that survives individual Claude Code session open/close cycles —
subsequent sessions attach to the running daemon instantly rather than
re-loading the embedding model each time.

After a code update, the daemon does NOT automatically pick up the new
binary. You need to restart it:

```bash
engram daemon stop   # graceful drain; waits for in-flight tool calls
# The next `engram serve` (i.e. the next Claude Code session) auto-spawns
# a fresh daemon from the updated binary.
```

The daemon auto-shuts-down after ``daemon.idle_shutdown_seconds``
(default 30 seconds) with no attached sessions, so closing all
Claude Code sessions causes the daemon to exit within about 30 seconds.

### Schema migrations

For most updates, `engram doctor` post-update is the catch-all: if any check is RED, the message tells you the remediation command. Watch for `embedding_model_changed` (run `engram reindex --full --model <new-model>`) or `schema_version_mismatch` (engram surfaces an explicit migration path). Skip the restart of your MCP client until doctor is all-green.

## Development

```bash
git clone https://github.com/kpachhai/engram  # pii-allow:repo-url
cd engram
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run mypy
```

The test suite (1300+ tests) covers unit, integration, and hermetic CLI smoke against the installed binary.

## Migrating from Open Brain

Existing Open Brain (OB1) corpus migrates in one command:

**Important:** the built-in `engram migrate-from-open-brain` MCP-based command does NOT work against any reasonably-recent OB1 deployment. OB1's MCP tools return human-readable text content, not structured records that engram can import. The recommended path today is direct Postgres access against OB1's Supabase `thoughts` table. See `docs/OPENBRAIN_MIGRATION_GUIDE.md` for a reference script + full walkthrough.

The migration:
- Bypasses MCP; reads the Supabase Postgres `thoughts` table directly.
- Reuses engram's existing `_migrate_one` pipeline (prefix parsing, fingerprint, atomic write, embedding).
- Is idempotent (re-run safely; matches existing thoughts on `(fingerprint, source, created_at)`).
- Generates `migration-report.json` with counts.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The project repo holds code only; user vaults live separately and never get pushed here.

## License

[Apache-2.0](LICENSE).
