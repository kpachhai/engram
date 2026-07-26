# ADR 002 - MCP tool surface: a frozen core, extended only additively

## Status

Accepted (Phase 1). Extended additively — see "Later additions" below.

## Later additions

The five-tool core decided here has since grown by additive, non-breaking
extension, exactly as the freeze clause below contemplates. None of the five
original tools changed shape.

| Added | Version | Tool |
|---|---|---|
| Optional LLM tools | v0.3.0 | `summarize_thought`, `synthesize_thoughts` (ADR 006) |
| Sixth core tool | v0.5.0 | `delete_thought(id, confirm)` — mandatory two-call confirmation |

The surface today is **eight tools: six core + two optional LLM**. The freeze
commitment is unchanged: existing tools never change shape within v1.x.

## Context

engram exposes its functionality to AI assistants over the Model Context Protocol. The tool surface is the API: any prompt, skill, or agent that targets engram is coupling to it. Stability matters more than completeness, because every breaking change forces every downstream prompt to re-test.

The Open Brain MCP server is the de-facto reference engram is replacing. Existing user prompts and skills already address its tool surface. Diverging from that surface for stylistic reasons would force a corpus of working prompts to be rewritten on day one.

## Decision

engram exposes exactly five tools, identical in name and shape to the Open Brain MCP surface:

| Tool | Purpose |
|---|---|
| `capture_thought(content, metadata?)` | Persist a thought. Embedding is best-effort; failures set `embedding_status='pending'`. |
| `search_thoughts(query, k?, filter?)` | ANN search over indexed thoughts. Excludes pending rows. |
| `list_thoughts(limit?, offset?, filter?, sort?)` | Paginated listing with filters and sort. |
| `thought_stats()` | Aggregate counts by prefix and portability. |
| `fetch(id)` | Return one thought by id. Returns null (not error) for unknown id. |

The surface is frozen for the v1.x lifetime. New capabilities go behind new tool names (e.g. `capture_thought_with_attachment`); existing tools never change shape.

## Consequences

### Positive

* Existing Open Brain prompts and skills work against engram with zero changes.
* The migration from Open Brain to engram is a data move, not a prompt rewrite.
* Frozen surface is a forcing function on tool design - new ideas land as new tools, not as breaking changes to existing ones.

### Negative

* Some tool shapes inherit Open Brain conventions that we might design differently from scratch (e.g. metadata-as-dict instead of named parameters). The cost of switching is paid once on day one if we change them; the cost of compatibility is zero.
* fetch's "null for unknown" semantics requires MCP clients to distinguish null from an error response. This is the right answer (it lets clients differentiate "no row" from "tool failed"), but it requires documentation discipline - the contract is in the schema.

## Alternatives considered

* **Bigger surface (Markdown export, vault management, embedding model swap as tools)** - rejected for v1. Out-of-band CLI commands (`engram reindex`, `engram doctor`, `engram migrate-from-open-brain`) cover these cases. MCP is for the read/write hot path; the CLI is for ops.
* **Smaller surface (just capture and search)** - rejected. `list_thoughts`, `thought_stats`, and `fetch` are load-bearing in the existing prompts; removing them would break working flows for cosmetic minimalism.
* **Versioned tool names from day one (`capture_thought_v1`)** - rejected. Premature; we have no v2 design pressure yet. If a v2 ever ships, it can introduce new tool names then.

## References

* `docs/superpowers/specs/2026-05-04-engram/02-TECHNICAL_DESIGN.md` MCP Tool Surface
* `src/engram/mcp/tools.py` - five pure async handlers
* `src/engram/mcp/server.py` - FastMCP wiring with `@mcp.tool` decorators
