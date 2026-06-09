"""Vault consolidation: report-then-action curation of the semantic index.

Detection passes (near-duplicate clusters, age-based stale candidates,
LLM-judged contradiction candidates, merge proposals) produce a
:class:`~engram.consolidate.models.ConsolidationReport` with zero vault
mutation. Apply executes merge proposals only: originals are archived
body-immutably under ``<vault>/archive/`` and the SQLite index is curated.

Design record: ``docs/adr/009-consolidation.md``.
"""
