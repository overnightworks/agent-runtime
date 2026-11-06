"""Shared arrangement for the MCP person-front suites."""

from __future__ import annotations

from agent_runtime_client.tools import ToolDeclaration, ToolEffect


def declaration(name: str, effect: ToolEffect = ToolEffect.READ_ONLY) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        description=f"{name} for tests",
        parameters={"type": "object", "properties": {"note": {"type": "string"}}},
        effect=effect,
    )
