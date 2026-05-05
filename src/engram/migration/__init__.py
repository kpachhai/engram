"""Open Brain -> engram migration command.

The :func:`engram.migration.open_brain.run_migration` function executes the
6-step pipeline defined in ``04-MIGRATION.md``:

1. Connect / Probe (verifies sort=created_at_asc parameter is accepted - B4 mitigation)
2. Enumerate (paginated list_thoughts)
3. Transform per thought (UUID-v7 mint, prefix parse, fingerprint compute,
   triple-match idempotency check, optional --prefer-legacy-id-match path)
4. Write markdown + insert SQLite + (optional) embedding
5. Validate via fetch(id) byte-for-byte (NOT semantic search; R13 mitigation)
6. Generate migration-report.json

Module layout:

* :mod:`engram.migration.open_brain` - core pipeline + HTTP MCP client.
* :mod:`engram.cli.migrate` - typer command.
"""

from __future__ import annotations

from engram.migration.open_brain import (
    MigrationConfig,
    MigrationReport,
    OpenBrainClient,
    OpenBrainThought,
    run_migration,
)

__all__ = [
    "MigrationConfig",
    "MigrationReport",
    "OpenBrainClient",
    "OpenBrainThought",
    "run_migration",
]
