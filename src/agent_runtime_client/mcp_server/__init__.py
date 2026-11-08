"""An app's MCP endpoint on revision 2026-07-28; it needs the ``mcp-server`` extra.

No base module of ``agent_runtime_client`` imports this package, so the base keeps its two
dependencies, pydantic and ruamel.yaml.
"""

from agent_runtime_client.mcp_server.person import (
    McpPersonFront,
    PersonRequest,
    PersonTool,
    PersonToolHandler,
    ResolvePerson,
)
from agent_runtime_client.mcp_server.resources import MemberResources
from agent_runtime_client.mcp_server.routes import RouteProfileError, RouteTools, route_tools

__all__ = [
    "McpPersonFront",
    "MemberResources",
    "PersonRequest",
    "PersonTool",
    "PersonToolHandler",
    "ResolvePerson",
    "RouteProfileError",
    "RouteTools",
    "route_tools",
]
