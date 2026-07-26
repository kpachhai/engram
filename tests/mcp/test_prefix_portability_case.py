"""BYOC-sensitive prefixes must not lose their default portability to case.

``Domain`` and ``Artifact`` default to ``portability=sensitive``, which is what
keeps them away from remote LLM providers. The prefix is parsed out of the
capture body without case normalization, so a lowercase ``[domain]`` used to
miss the defaults map and fall through to ``portable`` - quietly widening where
that thought may be sent.
"""

from __future__ import annotations

import pytest

from engram.mcp.tools import resolve_capture_metadata
from engram.models.mcp import CaptureInput, CaptureInputMetadata


@pytest.mark.parametrize("written_prefix", ["Domain", "domain", "DOMAIN", "dOmAiN"])
def test_domain_prefix_defaults_to_sensitive_regardless_of_case(written_prefix: str) -> None:
    resolved = resolve_capture_metadata(
        CaptureInput(content=f"[{written_prefix}] client runs a private fork"),
        default_user="tester",
    )
    assert resolved["portability"] == "sensitive", (
        f"[{written_prefix}] fell through to {resolved['portability']!r}; "
        "a BYOC-sensitive capture must not be downgraded by letter case"
    )


@pytest.mark.parametrize("written_prefix", ["Artifact", "artifact", "ARTIFACT"])
def test_artifact_prefix_defaults_to_sensitive_regardless_of_case(written_prefix: str) -> None:
    resolved = resolve_capture_metadata(
        CaptureInput(content=f"[{written_prefix}] internal deck outline"),
        default_user="tester",
    )
    assert resolved["portability"] == "sensitive"


def test_unrelated_prefix_still_defaults_to_portable() -> None:
    """The case fix must not widen the sensitive default to other prefixes."""
    resolved = resolve_capture_metadata(
        CaptureInput(content="[Lesson] tabs beat spaces"),
        default_user="tester",
    )
    assert resolved["portability"] == "portable"


def test_explicit_portability_still_wins_over_prefix_default() -> None:
    """An explicit caller-supplied portability is authoritative."""
    resolved = resolve_capture_metadata(
        CaptureInput(
            content="[domain] explicitly marked",
            metadata=CaptureInputMetadata(portability="block"),
        ),
        default_user="tester",
    )
    assert resolved["portability"] == "block"
