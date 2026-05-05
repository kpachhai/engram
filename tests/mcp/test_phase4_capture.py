"""Tests for ``CaptureInputMetadata.vault``.

(a) old metadata without vault field still validates,
(b) explicit vault arg routes through, and
(c) explicit vault to a non-mounted name refuses with
    RoutingTargetNotMounted (covered in routing tests).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engram.models.mcp import CaptureInput, CaptureInputMetadata


def test_phase3_metadata_without_vault_still_validates() -> None:
    """Pinned invariant 6: old clients keep working."""
    meta = CaptureInputMetadata(prefix="Lesson", source="cli")
    assert meta.vault is None


def test_phase4_metadata_with_vault_field() -> None:
    meta = CaptureInputMetadata(prefix="Postmortem", vault="team-x")
    assert meta.vault == "team-x"


def test_phase4_capture_input_round_trip_with_vault() -> None:
    inp = CaptureInput(
        content="[Postmortem] body",
        metadata=CaptureInputMetadata(vault="team-x"),
    )
    redumped = CaptureInput.model_validate(inp.model_dump())
    assert redumped.metadata is not None
    assert redumped.metadata.vault == "team-x"


def test_phase4_metadata_extra_field_still_forbidden() -> None:
    """extra="forbid" still applies after the additive change."""
    with pytest.raises(ValidationError):
        CaptureInputMetadata(unknown_field="oops")  # type: ignore[call-arg]


def test_phase4_metadata_round_trip_omits_unset_vault() -> None:
    """When vault is None, model_dump still serializes it (None) for symmetry."""
    meta = CaptureInputMetadata(prefix="Lesson")
    dumped = meta.model_dump()
    # vault should be present in dump (None), since it's a defined field.
    assert dumped["vault"] is None
