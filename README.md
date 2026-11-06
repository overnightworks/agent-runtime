# agent-runtime

The light client for apps that run agent turns on a model gateway. An app ships its
agents as files and declares its tools in code; the client sends each turn to the
app's LiteLLM gateway with the app's own key, runs the tool calls in the app and
streams the turn back as typed events. It also carries the run contract and an MCP
endpoint that verifies the run capability. One distribution,
`overnightworks-agent-runtime-client`, imported as `agent_runtime_client`.

## Install

The distribution is not on PyPI. Install the wheel that the
[latest release](https://github.com/overnightworks/agent-runtime/releases/latest)
lists: its page gives it as a `name @ URL#sha256=…` requirement for `pip install`
or `uv add`. A host may instead install by git tag:
`overnightworks-agent-runtime-client @ git+https://github.com/overnightworks/agent-runtime@v<version>`.

The `backend` extra adds the gateway backend (`httpx2`); the `mcp-server` extra adds an
app's MCP endpoint (PyJWT with cryptography).

## Where to read

| You are | Read |
|---|---|
| anyone new | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the client, its extras, trust |
| an app author running agent turns | [docs/wire.md](docs/wire.md): the run contract, agent files, the tool contract, the gateway backend, the MCP endpoint |

The code and its docstrings own behaviour, and `AGENTS.md` owns the layout, the boundaries, the checks and the
release rules.

## Checks

`AGENTS.md` lists the commands (sync, suite, ruff, `lint-imports`, build) and what CI
adds. The suite runs without a live provider.

## Licence

MIT — see [LICENSE](LICENSE).
