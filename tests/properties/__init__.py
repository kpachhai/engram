"""Hypothesis-driven property tests for storage and embedding invariants.

Per ``10-CODE_QUALITY.md``, these tests assert four high-value invariants:

* Capture-then-fetch round-trips content losslessly (modulo trailing newline).
* The body fingerprint is stable across whitespace-equivalent inputs.
* :func:`engram.storage.facade.VaultStorage.search` never returns more than
  ``k`` results.
* Reindex is idempotent: a second incremental pass over an already-indexed
  vault is a no-op (zero re-embeds, zero drift observations).
"""
