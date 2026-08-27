"""Wire-surface tests for the capture contract an MCP client can discover.

The consuming agent only sees what ``to_mcp_tool()`` advertises: the tool
description (from the docstring) and the input schema (from the parameter
types). These tests pin that surface so the capture contract - prefix
convention, portability values, metadata fields - stays reachable from the
seat that performs capture, and drifts red when the canonical prefix set
changes without the tool description following.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from engram.mcp.server import build_multivault_server, build_server
from engram.models.frontmatter import CANONICAL_PREFIXES
from engram.storage.facade import VaultStorage

DIM = 16

_METADATA_FIELDS = {"prefix", "portability", "source", "tags", "vault"}
_FILTER_FIELDS = {
    "prefix",
    "portability",
    "source",
    "tags",
    "vault",
    "created_after",
    "created_before",
}
_PORTABILITY_VALUES = ("portable", "sensitive", "block")


class _StubEmbedder:
    dimension: int = DIM
    model_name: str = "stub-model"

    def embed(self, text: str) -> list[float]:
        del text
        v = [0.0] * DIM
        v[0] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)


def _vault_storage(tmp_path: Path) -> VaultStorage:
    return VaultStorage(
        thoughts_dir=tmp_path / "thoughts",
        index_db_path=tmp_path / ".indexes" / "engram.db",
        embedding_dim=DIM,
    )


def _single_vault_server(tmp_path: Path):
    return build_server(
        _vault_storage(tmp_path),
        _StubEmbedder(),
        default_user="contract-tester",
    )


def _multivault_server(tmp_path: Path):
    from engram.config.models import (
        AggregatorConfig,
        EffectiveConfig,
        LLMConfig,
        SyncConfig,
    )
    from engram.llm.budget import LLMBudget
    from engram.mcp.llm_tools import HandlerDeps
    from engram.multivault.registry import VaultRegistry

    storage = _vault_storage(tmp_path)
    registry = VaultRegistry()
    registry.mount(name="primary", storage=storage, role="primary")
    config = EffectiveConfig(
        default_user="contract-tester",
        vault_path=tmp_path,
        thoughts_dir=tmp_path / "thoughts",
        index_dir=tmp_path / ".indexes",
        embedding_model="stub-model",
        vault_name="primary",
        sync=SyncConfig(),
        llm=LLMConfig(),
        aggregator=AggregatorConfig(),
    )
    deps = HandlerDeps(
        registry=registry,
        embedder=_StubEmbedder(),
        config=config,
        budget=LLMBudget(
            state_path=config.index_dir / "llm_usage.json",
            daily_cost_cap_usd=10.0,
        ),
    )
    return build_multivault_server(
        registry,
        _StubEmbedder(),
        deps,
        default_user="contract-tester",
    )


async def _tool_schema(server: Any, name: str) -> tuple[str, dict[str, Any]]:
    tool = await server.get_tool(name)
    mcp_tool = tool.to_mcp_tool()
    return mcp_tool.description or "", mcp_tool.inputSchema


@pytest.mark.parametrize("build", [_single_vault_server, _multivault_server])
async def test_capture_metadata_wire_schema_names_every_field(tmp_path, build):
    """The advertised metadata schema is the real typed model, not an opaque object."""
    description, schema = await _tool_schema(build(tmp_path), "capture_thought")
    del description
    model = schema.get("$defs", {}).get("CaptureInputMetadata")
    assert model is not None, f"metadata is not typed on the wire: {schema}"
    props = model.get("properties", {})
    assert set(props) == _METADATA_FIELDS
    assert model.get("additionalProperties") is False
    missing = [name for name in props if not _property_description(props[name])]
    assert not missing, f"metadata fields advertised without a description: {missing}"
    portability_desc = _property_description(props["portability"])
    for value in _PORTABILITY_VALUES:
        assert value in portability_desc, f"portability description omits {value!r}"


@pytest.mark.parametrize("build", [_single_vault_server, _multivault_server])
async def test_capture_description_covers_all_canonical_prefixes(tmp_path, build):
    """Drift gate: the tool description names every canonical prefix.

    Keyed to CANONICAL_PREFIXES so adding a 16th prefix without updating the
    advertised description goes red here.
    """
    description, _schema = await _tool_schema(build(tmp_path), "capture_thought")
    assert len(CANONICAL_PREFIXES) > 0, "canonical prefix tuple is empty"
    missing = [p for p in CANONICAL_PREFIXES if p not in description]
    assert not missing, f"capture_thought description omits canonical prefixes: {missing}"
    for value in _PORTABILITY_VALUES:
        assert value in description, f"capture_thought description omits {value!r}"


@pytest.mark.parametrize("build", [_single_vault_server, _multivault_server])
@pytest.mark.parametrize("tool_name", ["search_thoughts", "list_thoughts"])
async def test_filter_wire_schema_names_every_field(tmp_path, build, tool_name):
    """search/list advertise the real Filter model instead of an opaque object."""
    _description, schema = await _tool_schema(build(tmp_path), tool_name)
    model = schema.get("$defs", {}).get("Filter")
    assert model is not None, f"{tool_name} filter is not typed on the wire: {schema}"
    assert set(model.get("properties", {})) == _FILTER_FIELDS
    assert model.get("additionalProperties") is False


async def test_capture_response_echoes_resolved_portability_and_source(tmp_path):
    """A capture's resolved classification is visible to the agent that made it."""
    server = _single_vault_server(tmp_path)
    async with Client(server) as client:
        result = await client.call_tool(
            "capture_thought",
            {"content": "[Domain] wire contract echo probe"},
        )
    assert result.data["portability"] == "sensitive"  # Domain defaults to sensitive
    assert result.data["source"] == "contract-tester"


async def test_capture_response_echoes_explicit_metadata_override(tmp_path):
    server = _single_vault_server(tmp_path)
    async with Client(server) as client:
        result = await client.call_tool(
            "capture_thought",
            {
                "content": "[Lesson] explicit override probe",
                "metadata": {"portability": "block", "source": "someone-else"},
            },
        )
    assert result.data["portability"] == "block"
    assert result.data["source"] == "someone-else"


async def test_capture_rejects_unknown_metadata_key_on_the_wire(tmp_path):
    """The advertised additionalProperties=false matches enforced behavior."""
    from fastmcp.exceptions import ToolError

    server = _single_vault_server(tmp_path)
    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "capture_thought",
                {
                    "content": "[Lesson] junk key probe",
                    "metadata": {"portabilty": "block"},
                },
            )


def _property_description(prop: dict[str, Any]) -> str:
    """The description on a property schema (top level; anyOf branches carry none here)."""
    return str(prop.get("description", ""))
