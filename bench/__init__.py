"""Performance benchmarks for engram (NFR1).

Each benchmark targets a specific NFR1 number from
``docs/superpowers/specs/2026-05-04-engram/01-PRODUCT_SPEC.md``:

* Capture: <200ms p95 for typical thoughts (<2KB), warm model.
* Search top-10 over 10K thoughts: <100ms p95, warm model.
* Reindex 10K thoughts: <5 minutes on a 2024-era laptop.

Run with ``python -m bench.search_10k`` (or ``uv run python -m bench.search_10k``).
The benchmarks intentionally stay in pure stdlib + engram so they run in CI
without a heavyweight pytest harness.
"""
