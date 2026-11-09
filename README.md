# agents

A small client library for apps that run agent turns on a model gateway. An app ships its
agents as Markdown files and declares its tools in code. The client sends each turn to the
app's LiteLLM gateway with the app's own key, runs the tool calls in the app's process and
hands the turn back as typed events. It can also serve the app's tools to a signed-in
person's agent over MCP.

The distribution is `overnightworks-agent-runtime-client`, imported as `agent_runtime_client`.
It is a client only: no service, no provider CLI and no subscription login sit between an app
and its gateway.

## Install

The distribution is not on PyPI. A host pins the git tag:

`overnightworks-agent-runtime-client @ git+https://github.com/overnightworks/agent-runtime@v<version>`

The base depends on `pydantic` and `ruamel.yaml` only. Two extras are optional:

| Extra | Adds | For |
|---|---|---|
| `backend` | `httpx2` | `GatewayBackend`, the one backend, and `FakeGateway`, its scripted transport for tests |
| `mcp-server` | `anyio`, the official MCP SDK (`mcp`) | `agent_runtime_client.mcp_server`, an app's MCP endpoint on revision 2026-07-28 |

## Use

An app loads an agent file, builds the backend from its `agent` configuration section and
consumes the turn inside `aclosing`:

```python
from contextlib import aclosing
from importlib.resources import files

from agent_runtime_client.agents import load_agent
from agent_runtime_client.backend.gateway import GatewayBackend

agent = load_agent(files("my_app") / "agents" / "assistant.md", declarations)
backend = GatewayBackend(settings.agent, tools=hand_tools)

async with aclosing(backend.run(agent.request(messages), person)) as events:
    async for event in events:
        ...
```

`person` is a `TurnPerson`, always the verified person. A turn that ends short of its answer raises
`TurnStopped`, which says whether it was refused or failed and whether a side-effecting tool had
already started. [docs/connector.md](docs/connector.md) has the full contract.

## Where to read

| You are | Read |
|---|---|
| deciding whether this fits | [docs/VISION.md](docs/VISION.md): why it exists and what it is not |
| changing the client | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): building blocks, trust, constraints |
| an app author | [docs/connector.md](docs/connector.md): agent files, tools, the gateway backend, the MCP person front |
| an agent working here | [AGENTS.md](AGENTS.md): commands, traps, release rules |

The docstrings own the exact behaviour of each module.

## Licence

MIT - see [LICENSE](LICENSE).
