"""Storage layer for engram.

Two halves:

* Markdown source-of-truth (filesystem; ``engram.storage.markdown``).
* SQLite + sqlite-vec index (``engram.storage.sqlite``).

The atomicity contract (per ``02-TECHNICAL_DESIGN.md`` Flow A) is enforced by
the higher-level :class:`engram.storage.facade.VaultStorage` that composes
the two halves: markdown writes are durable before SQLite rows land; embedding
failure is non-fatal; SQLite mutation is wrapped in a transaction.
"""

from __future__ import annotations
