"""Light client for apps that run agents through the model gateway.

It owns what an app needs to run an agent: the agent file it ships
(``agent_runtime_client.agents``), the tool contract it declares (``agent_runtime_client.tools``),
the vocabulary of a turn (``agent_runtime_client.turn``) and the events a turn streams to its
caller (``agent_runtime_client.turn_events``), the backend that runs a turn against the gateway
(``agent_runtime_client.backend``) and the JSON-only MCP profile an app's own MCP endpoint speaks
(``agent_runtime_client.mcp_wire`` and ``agent_runtime_client.mcp_server``).
Its base depends on pydantic and ruamel.yaml only; the ``backend`` extra adds the HTTP library
for the gateway and the ``mcp-server`` extra adds anyio for ``agent_runtime_client.mcp_server``,
which no base module imports.
"""
