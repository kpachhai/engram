# engram vs Other AI Memory Tools

A clear-eyed look at what makes engram different — and when other tools are the better choice.

## TL;DR

| Tool | Storage SoT | MCP-native | Local embeddings | Multi-machine sync | Team-shared writes | License |
|---|---|---|---|---|---|---|
| **engram** | Markdown + SQLite | Yes (5+2 tools) | Yes (FastEmbed) | Yes (git) | Yes (GPG-attributed) | Apache-2.0 |
| Mem0 | Vector DB (Qdrant/pg) | Yes (cloud + self-host) | Optional | Cloud only | Cloud only | Apache-2.0 |
| Letta (MemGPT) | Postgres / SQLite | Yes | Internal tiered | DB replication | n/a (single tier) | Apache-2.0 |
| basic-memory | Markdown + SQLite | Yes (FastMCP) | Yes (FastEmbed) | git (DIY) | n/a (single project) | AGPL-3.0 |
| Open Brain (OB1) | Supabase + pgvector | Yes (Edge Function) | No (OpenRouter) | Cloud (multi-tenant SaaS) | n/a | Open source |
| Obsidian + Smart Connections | Markdown vault | Community plugins | Yes (bge-micro) | Obsidian Sync (paid) or community | n/a | Various |
| engraph | Markdown + SQLite | Yes (25 tools) | Yes (bundled llama.cpp) | git | n/a | MIT |
| Raw markdown + grep | Markdown only | No | No (keyword only) | git | DIY | n/a |

## Detailed comparisons

### vs Mem0

**Mem0 wins when:**

- You're building a production AI agent that needs autonomous memory management. Mem0 is built for "the agent decides what to remember" + has graph features (Neo4j integration) for entity relationships.
- Your team already uses LangChain / LlamaIndex / Vercel AI SDK and wants the integrations.
- You don't care about the data living in your filesystem — a managed cloud store is fine.

**engram wins when:**

- You want markdown source-of-truth so you can read every captured thought in any text editor.
- You need to run on a work laptop that blocks external services.
- You want git as the sync mechanism, not a Docker stack with Qdrant + optional Neo4j.
- You're optimizing for "the human decides what to remember" (BYOC discipline) rather than agent-driven capture.

**Why not Mem0:** Mem0 is solving production-agent memory; engram is solving personal-knowledge-worker memory. Different problems, both legitimate.

### vs Letta (formerly MemGPT)

**Letta wins when:**

- You need tiered memory (core / recall / archival) for long-running agents that exceed context window limits and need explicit memory management policies.
- Your application has cost / latency tradeoffs across memory tiers and you want the framework to manage them.
- You prefer a Postgres-backed deployment.

**engram wins when:**

- Your "agent" is a human at a keyboard, not a long-running autonomous process. Personal corpora fit comfortably in a single tier; tiering is overhead without payoff at this scale.
- You want markdown SoT.
- You want local-first with no DB to operate.

**Why not Letta:** Letta is research-grade infrastructure for stateful agents. Engram is a tool for humans who want their AI assistant to remember things.

### vs basic-memory

**basic-memory wins when:**

- You're already invested in the AGPL-3.0 ecosystem and the license is not a concern.
- You want the simplest possible markdown + MCP wrapper today, without engram's prefix taxonomy or portability discipline.
- You like its FTS + vector hybrid search.

**engram wins when:**

- You need Apache-2.0 licensing for compatibility with future enterprise distribution.
- You want first-class multi-vault from the start (basic-memory's vault story is single-project).
- You want the prefix taxonomy (`[Lesson]`, `[Friction]`, `[Decision]`, `[Postmortem]`, etc.) and portability classification (`portable` / `sensitive` / `block`) baked in.
- You want Open Brain MCP API parity.

**Why not basic-memory:** This was seriously considered as a build-on-top-of option. Three reasons engram is a separate implementation: (1) AGPL conflicts with future enterprise paths, (2) basic-memory's schema doesn't naturally fit engram's prefix-as-subdirectory + portability-tag-as-frontmatter model, (3) tying release cadence to basic-memory constrains engram's roadmap (especially team-vault patterns). Credit where due — engram borrows basic-memory's storage recipe.

### vs Open Brain (OB1)

**Open Brain wins when:**

- You already have it running and your data is in it. Migration cost is real.
- Cloud-hosted multi-device-by-default is more convenient than git sync.
- You like Supabase Studio for browsing thoughts.

**engram wins when:**

- You want to escape the per-month OpenRouter spend.
- You need to run on a work laptop where Open Brain is unreachable.
- You want markdown SoT — Open Brain's data lives in Postgres rows you can't read in any editor.
- You want git history of every capture/edit/delete (Open Brain has none).

**Why engram is the natural successor:** Open Brain proved the MCP-tool-surface design but the SaaS architecture is the wrong shape for personal AI memory. Engram preserves the API surface (drop-in compatibility for existing Open Brain consumers) while replacing the substrate. The included `engram migrate-from-open-brain` command paginates through your existing corpus and writes one markdown file per thought.

### vs Obsidian + Smart Connections

**Obsidian wins when:**

- You want a polished GUI for browsing your knowledge.
- You're already an Obsidian user and have hundreds of plugins configured.
- You like the wiki-style `[[link]]` graph and the Canvas plugin.

**engram wins when:**

- You need a HEADLESS backend that runs on a work laptop without an app open.
- You want Open Brain MCP API parity for AI clients.
- You want git as the sync mechanism (Obsidian Sync is paid; community plugins vary).

**They compose:** point Obsidian at the same `thoughts/` directory engram serves. You browse in Obsidian, your AI captures via engram. Both speak markdown.

**Why not Obsidian + Smart Connections:** the GUI dependency is the blocker on locked-down work machines. Engram is the memory backend; Obsidian is the browser. Use both.

### vs engraph

**engraph wins when:**

- You want a single Rust binary with no Python install required.
- You want bundled llama.cpp embeddings (no separate model download).
- You like its hybrid 5-lane search (semantic + FTS + graph + reranker + temporal).

**engram wins when:**

- You want Open Brain MCP API parity (engraph exposes 25 tools; Open Brain expects 5).
- You want the prefix taxonomy + portability discipline baked in.
- You prefer Python ecosystem (easier LLM integration, larger community).

**Why not engraph:** engraph is the most architecturally similar surveyed tool and is genuinely tempting. The 25-tool surface is too wide to map cleanly to Open Brain's 5-tool surface, and going Rust would slow delivery. Engraph informs any future engram-rust port (especially the bundled-llama.cpp pattern and multi-lane search).

### vs LlamaIndex DIY stack

**LlamaIndex wins when:**

- You want maximum flexibility — pick every component (vector store, embedding model, MCP wrapper).
- You're already deep in the LlamaIndex ecosystem.

**engram wins when:**

- You want opinionated defaults that work today, not a library you assemble into a tool.
- You don't want to depend on LlamaIndex's release cycle.

**Why not LlamaIndex:** LlamaIndex is a library, not a tool. Building engram on top would still require all the same decisions (which vector store, which markdown loader, which MCP wrapper) without saving meaningful work. Engram uses lower-level libraries (FastMCP, FastEmbed, sqlite-vec) directly for tighter dependency control.

### vs Raw markdown + grep

**Raw + grep wins when:**

- You have under ~100 thoughts. Grep is fine; engram is overkill.
- You don't have a daily AI workflow yet.

**engram wins when:**

- You've crossed the threshold where keyword search misses paraphrases, synonyms, conceptually-related thoughts.
- You want MCP integration so an AI assistant can search your memory autonomously.
- You want metadata querying (filter by prefix, portability, source, date) on top of semantic search.

**Why not just grep:** the friction-log baseline (one append-only markdown file with grep-able content) works for under 100 thoughts. Past that, semantic search is a step-change in usefulness. Engram is the upgrade path from friction-log without losing friction-log's "it's just files" virtue.

## What engram is NOT trying to replace

To be clear about scope:

- **Not a wiki / KM tool.** Notion, Confluence, Obsidian as a wiki — those are general-purpose KM and they win at curated, reviewed, link-graph-navigable knowledge. Engram is the **capture layer**; wiki tools are the **curation layer**. Promoting an engram thought into a curated wiki page is a deliberate authoring step. Most teams need both.
- **Not a vector DB for production apps.** Pinecone, Weaviate, Qdrant exist for production embedding workloads. Engram is personal memory; the workload shape is different.
- **Not a note-taking app.** Apple Notes, Bear, Drafts win at general note-taking. Engram is for AI-mediated capture and retrieval.
- **Not a backup tool.** `git push` is engram's backup; backup tools serve a different layer.

## What engram borrows from each tool

| From | What engram borrows |
|---|---|
| Open Brain | MCP tool surface, prefix taxonomy, portability discipline |
| basic-memory | Storage recipe (markdown + SQLite + FastEmbed + FastMCP), atomic writes, schema validation |
| engraph | Bundled-embeddings deployment story (target for any future Rust port), file watching for external edits |
| Obsidian | Markdown frontmatter conventions, vault terminology, multi-vault model |
| Letta | Audit log structure (future enterprise enhancement) |
| Mem0 | Source attribution patterns for multi-source corpora |
| Logseq | Org-mode-friendly markdown (compatible with Logseq's daily journal patterns) |

## Decision tree

1. **Are you running an autonomous production AI agent that decides what to remember?** Use Mem0 or Letta.
2. **Are you on Windows without WSL?** Use basic-memory or engraph.
3. **Do you want a single Rust binary with no Python install?** Use engraph.
4. **Do you have under 50 captured thoughts and want zero infrastructure?** Use a single markdown file + grep until you cross the threshold.
5. **Do you want a polished GUI to browse your knowledge?** Use Obsidian, Logseq, or another markdown-based note-taking app — and run engram alongside them on the same `thoughts/` directory if you want AI integration.
6. **Anything else — solo knowledge worker, multi-machine personal user, friend-share, or small team?** engram is built for you.
