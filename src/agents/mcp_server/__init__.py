"""An app's MCP endpoint on revision 2026-07-28; it needs the ``mcp-server`` extra.

No base module of ``agents`` imports this package, so the base keeps its two
dependencies, pydantic and ruamel.yaml.
"""

from agents.mcp_server.listen import ResourceChanges
from agents.mcp_server.person import (
    McpPersonFront,
    PersonRequest,
    PersonTool,
    PersonToolHandler,
    ResolvePerson,
)
from agents.mcp_server.resources import MemberResources
from agents.mcp_server.routes import RouteProfileError, RouteTools, route_tools

__all__ = [
    "McpPersonFront",
    "MemberResources",
    "PersonRequest",
    "PersonTool",
    "PersonToolHandler",
    "ResolvePerson",
    "ResourceChanges",
    "RouteProfileError",
    "RouteTools",
    "route_tools",
]
