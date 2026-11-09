"""Light client for apps that run agents through the model gateway.

It owns what an app needs to run an agent: the agent file it ships
(``agents.agent_file``), the tool contract it declares (``agents.tools``),
the vocabulary of a turn (``agents.turn``) and the events a turn streams to its
caller (``agents.turn_events``), the backend that runs a turn against the gateway
(``agents.backend``) and an app's own MCP endpoint, serving revision 2026-07-28
(``agents.mcp_wire`` and ``agents.mcp_server``).
Its base depends on pydantic and ruamel.yaml only; the ``backend`` extra adds the HTTP library
for the gateway and the ``mcp-server`` extra adds anyio and the official MCP SDK for
``agents.mcp_server``, which no base module imports.
"""
