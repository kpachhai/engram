# Feature Plan: delete_thought — MCP Tool + CLI Command

**Status**: Planned (not yet implemented)
**Created**: 2026-05-13
**Priority**: High — completes the CRUD surface; current workaround (rm + reindex) is error-prone

---

## 1. Context and motivation

Engram currently exposes seven MCP tools. Five are deterministic core tools:
`capture_thought`, `search_thoughts`, `list_thoughts`, `thought_stats`, `fetch`.
Two are optional LLM tools: `summarize_thought`, `synthesize_thoughts`.

There is **no deletion path**. The workaround today is:

```bash
rm <vault>/thoughts/<prefix>/<year>/<file>.md
engram reindex --remove-orphans
```

This is fragile: it leaves a stale SQLite row + embedding until `reindex` runs,
the thought still appears in search results in the window between the two
commands, and there is no audit trail. There is also no way for an AI assistant
to help a user find and remove thoughts during a conversation — the AI can
search but cannot complete the action.

This plan adds:

1. **`delete_thought(id, confirm)` MCP tool** — allows an AI session to
   complete a search-then-delete flow atomically and safely.
2. **`engram delete <id>` CLI command** — allows operators to delete from a
   terminal with an explicit confirmation gate.

Both paths remove the markdown file AND the SQLite row + embedding atomically
and enqueue a git commit via `SyncCoordinator`, so the deletion propagates to
other machines on the next `git pull`.

---

## 2. Guardrail design (read this before implementing anything)

Deletion is irreversible. Once the markdown file is gone and the git commit
is pushed, recovery requires `git log` archaeology. The feature must be
designed so that accidental deletion by a rogue agent, a mistyped command,
or a confused AI response is structurally impossible — not just unlikely.

### 2.1 MCP tool guardrail: mandatory `confirm` parameter

```python
delete_thought(id: str, confirm: bool) -> DeleteOutput
```

- `confirm` is a **required** parameter with **no default value**. Callers must
  explicitly pass it.
- When `confirm=False`: the handler returns a **preview** — the thought's
  metadata (prefix, portability, created_at, first 200 chars of body) and a
  clear message: `"Dry run: thought not deleted. Call again with confirm=True
  to delete."`. Nothing is modified.
- When `confirm=True`: deletion proceeds.
- The AI **must always show the preview to the user** before calling with
  `confirm=True`. The system prompt / CLAUDE.md should document this contract.
  (Use `confirm=False` first, show the user the result, get explicit approval,
  then call `confirm=True`.)
- The tool description string (shown to AI clients) must say:
  `"ALWAYS call with confirm=False first to preview the thought. Only call with
  confirm=True after the user has explicitly approved the deletion."`

### 2.2 CLI guardrail: explicit typed confirmation

```bash
engram delete <id>
```

- Shows the full thought content (not just metadata) in the terminal.
- Prompts: `"Type 'delete' to confirm permanent deletion, or Ctrl-C to abort: "`
- Accepts only the literal string `"delete"` (case-sensitive). Any other input
  aborts.
- `--yes` flag bypasses the prompt for scripted use. Must be documented as
  "dangerous; intended for CI/scripts that have already validated the ID."
- `--dry-run` shows what would be deleted without prompting or deleting.

### 2.3 Audit log

Every deletion (both MCP and CLI paths) must emit a structured log line at
INFO level:

```
engram.storage.facade  INFO  thought_deleted id=<uuid> prefix=<prefix>
  portability=<p> fingerprint=<sha256> vault=<name> source=<mcp|cli>
```

The daemon log already captures this (rotating, 7-day retention). This gives
the operator a forensic trail for "who deleted what and when."

### 2.4 Git commit via SyncCoordinator

Deletion must enqueue a git commit via `storage._post_capture_sync()` (or an
equivalent path). The `git rm` for the thought file should be committed with
message `"engram: delete thought <prefix>/<id[:8]>"`. This makes deletion
visible in `git log` and recoverable via `git checkout <sha> -- <path>`.

### 2.5 No bulk deletion via MCP

The MCP tool accepts a **single `id`**, not a list. Bulk deletion via the AI
requires N explicit tool calls with N explicit `confirm=True` values — one
confirmation per thought. This is intentional friction.

The CLI may expose `--from-file <ids.txt>` in a future iteration, but that is
out of scope for this plan.

---

## 3. API design

### 3.1 MCP tool: `delete_thought`

Input model (add to `src/engram/models/mcp_io.py`):

```python
class DeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., description="UUID of the thought to delete.")
    confirm: bool = Field(
        ...,
        description=(
            "Set False for a dry-run preview. Set True only after the user has "
            "explicitly approved the deletion shown in the preview response."
        ),
    )
```

Output model:

```python
class DeleteOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    deleted: bool
    id: str
    prefix: str | None = None
    portability: str | None = None
    created_at: str | None = None
    body_preview: str | None = None   # first 200 chars; present in dry-run
    message: str                       # human-readable status line
```

### 3.2 CLI command

```
engram delete <id> [--dry-run] [--yes] [--vault <name>] [--config <path>]
```

| Flag | Meaning |
|---|---|
| `<id>` | UUID of the thought (required) |
| `--dry-run` | Show what would be deleted; do not delete |
| `--yes` | Skip interactive confirmation (scripts only) |
| `--vault` | Target vault name (defaults to primary) |
| `--config` | Path to user config (defaults to `~/.config/engram/config.yaml`) |

Exit codes: `0` deleted (or dry-run completed), `1` not found or aborted,
`2` config/vault error.

---

## 4. Storage layer changes

`VaultStorage.delete()` already exists in `src/engram/storage/facade.py`.
Verify it:

1. Unlinks the markdown file.
2. Deletes the SQLite row from `thoughts` and the embedding row from
   `thought_embeddings`.
3. Returns the deleted `Thought` object (for the audit log and the MCP
   output preview).
4. Raises `ThoughtNotFoundError` (add to `src/engram/errors.py`) when the
   ID does not exist in SQLite.
5. Emits the audit log line (add if not present).
6. Calls `_post_capture_sync()` with the deleted file path so
   `SyncCoordinator` enqueues a git commit.

If any of these are missing, add them as part of this feature.

---

## 5. Implementation steps (ordered)

Follow this order exactly. Each step is a self-contained unit; commit after
each if it passes lint + type-check.

### Step 1 — Add `ThoughtNotFoundError` to `src/engram/errors.py`

```python
class ThoughtNotFoundError(EngramError):
    """Raised when a delete or fetch targets an ID not in the index."""
    error_code = "thought_not_found"
```

Add to `__all__`.

### Step 2 — Harden `VaultStorage.delete()` in `src/engram/storage/facade.py`

Verify the method:
- Fetches the `Thought` row first (raises `ThoughtNotFoundError` if absent).
- Unlinks the markdown file (`Path(thought.file_path).unlink(missing_ok=True)`).
- Deletes both SQLite rows atomically (one transaction).
- Emits the structured audit log line.
- Calls `self._post_capture_sync(thought.file_path)` so the coordinator
  enqueues a git commit for the deletion.
- Returns the `Thought` object that was deleted.

Do NOT modify the return type signature of the public API if it already returns
`Thought`; if it currently returns `None`, change it to `Thought`.

### Step 3 — Add `DeleteInput` and `DeleteOutput` to `src/engram/models/mcp_io.py`

Per the models in Section 3.1. Add to `__all__`.

### Step 4 — Add the `delete_thought` MCP handler to `src/engram/mcp/tools.py`

```python
async def handle_delete_thought(
    storage: VaultStorage,
    input_data: DeleteInput,
) -> DeleteOutput:
    thought = storage.get_by_id(input_data.id)
    if thought is None:
        return DeleteOutput(
            deleted=False,
            id=input_data.id,
            message=f"Not found: no thought with id={input_data.id!r}",
        )
    preview = DeleteOutput(
        deleted=False,
        id=thought.id,
        prefix=thought.prefix,
        portability=thought.portability,
        created_at=thought.created_at.isoformat() if thought.created_at else None,
        body_preview=thought.body[:200] if thought.body else None,
        message=(
            "Dry run: thought not deleted. "
            "Call again with confirm=True to permanently delete."
        ),
    )
    if not input_data.confirm:
        return preview
    storage.delete(input_data.id)  # raises ThoughtNotFoundError if race-deleted
    return DeleteOutput(
        deleted=True,
        id=thought.id,
        prefix=thought.prefix,
        portability=thought.portability,
        created_at=thought.created_at.isoformat() if thought.created_at else None,
        message=f"Deleted thought {thought.id} ({thought.prefix}).",
    )
```

### Step 5 — Register `delete_thought` on the FastMCP server in `src/engram/mcp/server.py`

Add the tool registration alongside the other five core tools. Tool description
string **must** include the confirmation protocol warning (see Section 2.1).
Example:

```python
@mcp.tool(
    description=(
        "Delete a thought permanently. "
        "ALWAYS call with confirm=False first to preview the thought and show "
        "the user what will be deleted. Only call with confirm=True after the "
        "user has explicitly approved the deletion. "
        "Returns metadata of the deleted thought."
    )
)
async def delete_thought(id: str, confirm: bool) -> dict:
    ...
```

Update the tool count in `docs/ARCHITECTURE.md` (seven → eight tools) and
`README.md` (tools table).

### Step 6 — Add `engram delete` CLI command to `src/engram/cli/`

Create `src/engram/cli/delete.py` (or add to an appropriate existing module).
Wire it into the Typer app in `src/engram/cli/__init__.py`.

The command must:
1. Load config + open `VaultStorage` (follow the pattern in `engram/cli/serve.py`).
2. Fetch the thought by ID; exit 1 with a clear message if not found.
3. Print the thought's prefix, portability, created_at, and full body.
4. If `--dry-run`: print "Dry run — nothing deleted." and exit 0.
5. If not `--yes`: prompt `"Type 'delete' to confirm permanent deletion, or Ctrl-C to abort: "`. Read stdin. If input != `"delete"`, print "Aborted." and exit 1.
6. Call `storage.delete(id)`.
7. Print `"Deleted: <prefix>/<id[:8]>"` and exit 0.

### Step 7 — Update `src/engram/cli/__init__.py`

Register the new `delete` subcommand so it appears in `engram --help`.
Add a hermetic CLI smoke in `tests/test_phase5_cli_smoke.py`:
`engram delete --help` → exit 0, output contains `--dry-run` and `--yes`.

---

## 6. Test plan

All tests go under `tests/`. Follow existing naming conventions.

### 6.1 Unit tests — `tests/storage/test_delete.py`

| Test | What it verifies |
|---|---|
| `test_delete_removes_markdown_file` | After `storage.delete(id)`, the `.md` file no longer exists on disk |
| `test_delete_removes_sqlite_row` | After delete, `storage.get_by_id(id)` returns `None` |
| `test_delete_removes_embedding_row` | After delete, `thought_embeddings` has no row for the ID (query directly) |
| `test_delete_returns_thought_object` | The returned object matches the thought that was deleted |
| `test_delete_nonexistent_raises` | `ThoughtNotFoundError` raised for an unknown ID |
| `test_delete_enqueues_sync` | When a coordinator is attached, `coordinator.enqueue` is called with the deleted file path |
| `test_delete_emits_audit_log` | Audit log line appears in `caplog` at INFO level |

### 6.2 Unit tests — `tests/mcp/test_delete_thought.py`

| Test | What it verifies |
|---|---|
| `test_dry_run_returns_preview` | `confirm=False` → `deleted=False`, body_preview populated, nothing deleted |
| `test_confirm_true_deletes` | `confirm=True` → `deleted=True`, storage.delete called |
| `test_not_found_returns_error_output` | Unknown ID → `deleted=False`, message contains "Not found" |
| `test_dry_run_does_not_call_storage_delete` | Asserts storage.delete is never called when `confirm=False` |

### 6.3 Integration test — `tests/integration/test_delete_flow.py`

End-to-end: capture → `search_thoughts` finds it → `delete_thought(confirm=False)` returns preview → `delete_thought(confirm=True)` → `fetch` returns None → `search_thoughts` no longer returns the thought.

### 6.4 CLI smoke — `tests/test_phase5_cli_smoke.py`

Add to the existing smoke file:
- `engram delete --help` exits 0 and mentions `--dry-run`, `--yes`.
- `engram delete <nonexistent-id>` exits 1 and prints a "not found" message.
- `engram delete <id> --dry-run` exits 0 and does not modify the vault.

---

## 7. Documentation updates

Update ALL of the following. Do not skip any.

| File | What to add/change |
|---|---|
| `docs/ARCHITECTURE.md` — MCP API surface table | Add `delete_thought` row: "Delete a thought by id. Side effects: unlinks markdown, deletes SQLite row + embedding, enqueues git commit." |
| `docs/ARCHITECTURE.md` — tool count | "Seven tools" → "Eight tools" |
| `README.md` — Tools exposed via MCP | Add `delete_thought(id, confirm)` bullet with the dry-run contract described |
| `README.md` — tool count line | Update count |
| `CLAUDE.md` — MCP server bullet | Note that `delete_thought` requires `confirm=False` preview before `confirm=True` |
| `CHANGELOG.md` — [Unreleased] section | Add under `### Added`: `delete_thought` MCP tool + `engram delete` CLI with confirmation gate |
| `docs/DAEMON_MODE.md` — no change needed | — |

---

## 8. Pinned-invariant compliance check

Before merging, verify each invariant is still satisfied:

| # | Invariant | Impact |
|---|---|---|
| 1 | Markdown SoT | Delete removes the markdown file — this is correct, markdown is the source of truth for what exists |
| 2 | SQLite is regenerable | Delete removes the SQLite row — correct, the row no longer has a markdown backing |
| 3 | No direct MCP client breaking changes | `delete_thought` is an additive new tool; existing tool signatures unchanged |
| 4 | portability=block never reaches LLM | Not affected by delete |
| 5 | Sync coordinator drains on shutdown | Delete enqueues on coordinator — coordinator drains it on shutdown per existing contract |
| 6 | MCP wire format stable | New tool is additive; not breaking |

---

## 9. Exit criteria

This feature is done when ALL of the following are true:

- [ ] `engram delete --help` works and documents `--dry-run` and `--yes`
- [ ] `engram delete <id>` shows thought content and prompts for typed `"delete"` confirmation
- [ ] `engram delete <id> --dry-run` exits 0, prints preview, does NOT modify vault
- [ ] `delete_thought(id, confirm=False)` returns preview, does NOT modify vault
- [ ] `delete_thought(id, confirm=True)` removes markdown file, SQLite row, embedding
- [ ] Deletion enqueues a git commit via `SyncCoordinator`
- [ ] Audit log line emitted for every deletion
- [ ] `ThoughtNotFoundError` raised/returned for unknown IDs
- [ ] `ruff check`, `ruff format --check`, `mypy` all pass
- [ ] `pytest` passes (all existing + new tests)
- [ ] Coverage gate (80%) still passes
- [ ] All documentation files listed in Section 7 are updated
- [ ] Pinned-invariant table in Section 8 verified

---

## 10. Out of scope for this plan

- Bulk delete (delete-by-search-query) — too high blast radius; defer
- Soft-delete / trash folder — valid future enhancement
- Undo / restore command — valid future enhancement; requires storing deleted thought metadata
- Delete from a specific vault in a multi-vault deployment (use `--vault` flag on CLI; MCP tool routes to primary by default, same as capture)
