"""``engram summarize`` and ``engram synthesize`` CLI commands.

Thin wrappers around the LLM-mediated MCP handlers in
:mod:`engram.mcp.llm_tools`. The handlers do all the work (provider
resolution, budget enforcement, portability gating, citation
validation); this module is the CLI shim so operators can invoke
the same functionality from a terminal without going through an MCP
client.

The two LLM-backed operations exposed here:

* ``engram summarize <thought-id>`` - LLM compresses a single thought.
* ``engram synthesize "<query>"`` - LLM synthesizes from cross-vault
  search results, with citations.

Both commands honor the same provider config + portability rules as
the MCP tools: ``portability=block`` thoughts NEVER reach an LLM;
``portability=sensitive`` thoughts only reach local providers (Ollama
/ llama.cpp); the daily cost cap is enforced; LLM failures surface
as non-zero exit + a clear error message.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import typer

from engram.errors import (
    BlockThoughtLLMDisallowed,
    EngramError,
    LLMProviderError,
)

if TYPE_CHECKING:
    from engram.embedding.protocol import EmbeddingProvider
    from engram.mcp.llm_tools import HandlerDeps
    from engram.multivault.registry import VaultRegistry


def register(app: typer.Typer) -> None:
    """Wire ``engram summarize`` and ``engram synthesize`` into the Typer app."""

    @app.command("summarize")
    def summarize_cmd(
        thought_id: str = typer.Argument(
            ...,
            help="UUID of the thought to summarize.",
        ),
        config_path: Path | None = typer.Option(  # noqa: B008
            None,
            "--config",
            help="Path to a vault's engram.config.yaml.",
        ),
        vault_name: str | None = typer.Option(
            None,
            "--vault",
            help="Vault alias from the per-user vaults: list.",
        ),
        as_json: bool = typer.Option(
            False,
            "--json",
            help="Emit the full SummarizeOutput as JSON instead of plain text.",
        ),
    ) -> None:
        """LLM-mediated summary of a single thought.

        Refuses to send ``portability=block`` thoughts to any provider;
        sends ``portability=sensitive`` thoughts only to local providers
        (Ollama, llama.cpp). Honors the daily cost cap configured under
        ``llm.daily_cost_cap_usd``.
        """
        try:
            tid = UUID(thought_id)
        except ValueError as exc:
            typer.echo(f"error: {thought_id!r} is not a valid UUID", err=True)
            raise typer.Exit(2) from exc

        try:
            deps, _registry, _embedder = _build_handler_deps(
                config_path=config_path,
                vault_name=vault_name,
            )
            from engram.mcp.llm_tools import (
                SummarizeInput,
                summarize_thought_handler,
            )

            result = asyncio.run(
                summarize_thought_handler(
                    deps,
                    payload=SummarizeInput(id=tid),
                ),
            )
        except (BlockThoughtLLMDisallowed, LLMProviderError, EngramError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc

        if as_json:
            typer.echo(result.model_dump_json(indent=2))
        else:
            typer.echo(result.summary)
            if result.citations:
                typer.echo("")
                typer.echo("Citations:")
                for cid in result.citations:
                    typer.echo(f"  - {cid}")
            if result.cost_usd > 0:
                typer.echo(
                    f"\n(input={result.input_tokens}t, output={result.output_tokens}t, "
                    f"cost=${result.cost_usd:.4f})",
                    err=True,
                )

    @app.command("synthesize")
    def synthesize_cmd(
        query: str = typer.Argument(
            ...,
            help="Natural-language query.",
        ),
        k: int = typer.Option(
            10,
            "--k",
            help="Number of top-k results to retrieve before synthesis.",
            min=1,
            max=50,
        ),
        config_path: Path | None = typer.Option(  # noqa: B008
            None,
            "--config",
            help="Path to a vault's engram.config.yaml.",
        ),
        vault_name: str | None = typer.Option(
            None,
            "--vault",
            help="Vault alias from the per-user vaults: list.",
        ),
        vault_filter: str | None = typer.Option(
            None,
            "--vault-filter",
            help=(
                "Restrict cross-vault search: '*' means all mounted vaults; "
                "'<name>' targets a single vault; default is primary only."
            ),
        ),
        include_sensitive: bool = typer.Option(
            False,
            "--include-sensitive",
            help=(
                "Allow sensitive thoughts in the synthesis context (requires a local LLM provider)."
            ),
        ),
        include_friend_vaults: bool = typer.Option(
            False,
            "--include-friend-vaults",
            help=(
                "Include thoughts originating from imported friend bundles "
                "(default: drop them so cross-trust attribution stays clean)."
            ),
        ),
        as_json: bool = typer.Option(
            False,
            "--json",
            help="Emit the full SynthesizeOutput as JSON.",
        ),
    ) -> None:
        """LLM-mediated cross-vault synthesis with citations.

        Aggregates top-k results from mounted vaults, applies the
        portability gate, wraps each thought in delimited blocks, and
        invokes the configured LLM provider with an anti-injection
        system prompt. Citations are post-validated against the
        retrieved set.
        """
        try:
            deps, _registry, _embedder = _build_handler_deps(
                config_path=config_path,
                vault_name=vault_name,
            )
            from engram.mcp.llm_tools import (
                SynthesizeInput,
                synthesize_thoughts_handler,
            )
            from engram.models.mcp import Filter

            filter_obj = Filter(vault=vault_filter) if vault_filter is not None else None
            result = asyncio.run(
                synthesize_thoughts_handler(
                    deps,
                    payload=SynthesizeInput(
                        query=query,
                        k=k,
                        filter=filter_obj,
                        include_sensitive=include_sensitive,
                        include_friend_vaults=include_friend_vaults,
                    ),
                ),
            )
        except (BlockThoughtLLMDisallowed, LLMProviderError, EngramError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc

        if as_json:
            typer.echo(result.model_dump_json(indent=2))
        else:
            typer.echo(result.answer)
            if result.citations:
                typer.echo("")
                typer.echo("Citations:")
                for cid in result.citations:
                    typer.echo(f"  - {cid}")
            if result.degraded_vaults:
                typer.echo(
                    f"\n(degraded vaults: {', '.join(result.degraded_vaults)})",
                    err=True,
                )
            if result.cost_usd > 0:
                typer.echo(
                    f"\n(input={result.input_tokens}t, output={result.output_tokens}t, "
                    f"cost=${result.cost_usd:.4f})",
                    err=True,
                )


def _build_handler_deps(
    *,
    config_path: Path | None,
    vault_name: str | None,
) -> tuple[HandlerDeps, VaultRegistry, EmbeddingProvider]:
    """Construct the HandlerDeps + registry + embedder used by both commands.

    Loads the per-user config, mounts every vault, builds the LLM budget,
    and returns a HandlerDeps the LLM handlers can use directly. The
    caller is responsible for closing storages (this CLI is single-shot
    so we let process exit handle it).
    """
    from engram.config.loader import _load_user_config_if_present, load_config
    from engram.embedding.fastembed import FastEmbedProvider
    from engram.llm.budget import LLMBudget, usage_state_path_for
    from engram.mcp.llm_tools import HandlerDeps
    from engram.multivault.registry import VaultRegistry
    from engram.storage.facade import VaultStorage
    from engram.storage.sqlite import set_setting

    config = load_config(
        explicit_vault_config=config_path,
        vault_name=vault_name,
    )
    user_config = _load_user_config_if_present()

    embedder = FastEmbedProvider(model_name=config.embedding_model)
    registry = VaultRegistry()

    # Mount the targeted vault as primary.
    primary_storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=embedder.dimension,
        embedding_model_name=config.embedding_model,
        vault_name=config.vault_name,
    )
    set_setting(primary_storage.conn, "embedding_model_name", config.embedding_model)
    set_setting(primary_storage.conn, "embedding_dim", str(embedder.dimension))
    registry.mount(name=config.vault_name, storage=primary_storage, role="primary")

    # Mount any additional vaults from the per-user config.
    if user_config is not None:
        for mount in user_config.vaults:
            if mount.name == config.vault_name:
                continue
            vault_path = mount.path.expanduser().resolve()
            if not vault_path.exists():
                continue
            storage = VaultStorage(
                thoughts_dir=vault_path / "thoughts",
                index_db_path=vault_path / ".indexes" / "engram.db",
                embedding_dim=embedder.dimension,
                embedding_model_name=config.embedding_model,
                vault_name=mount.name,
            )
            set_setting(storage.conn, "embedding_model_name", config.embedding_model)
            set_setting(storage.conn, "embedding_dim", str(embedder.dimension))
            registry.mount(name=mount.name, storage=storage, role=mount.role)

    state_path = usage_state_path_for(primary_vault_index_dir=config.index_dir)
    budget = LLMBudget.load_or_init(
        state_path=state_path,
        daily_cost_cap_usd=config.llm.daily_cost_cap_usd,
    )

    deps = HandlerDeps(
        registry=registry,
        embedder=embedder,
        config=config,
        budget=budget,
    )
    return deps, registry, embedder


__all__ = ["register"]
