"""Diagnostics surface for engram - the doctor command's machinery.

The :func:`engram.diagnostics.doctor.run_diagnostics` function returns a
:class:`engram.diagnostics.doctor.DoctorReport` covering config validity,
filesystem permissions, SQLite + sqlite-vec, embedding model state, and
index-vs-disk drift. The :mod:`engram.cli.doctor` module is the user-facing
typer command that calls into this.
"""

from __future__ import annotations

from engram.diagnostics.doctor import (
    CheckResult,
    CheckStatus,
    DoctorReport,
    run_diagnostics,
)

__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "run_diagnostics",
]
