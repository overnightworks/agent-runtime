# agents

## Commands

- `uv sync --locked --extra dev`: install the environment; run it before any other command.
- `uv run pytest -q -n auto`: the suite; needs no live provider.
- `uv run ruff check src tests`: lint `src` and `tests`.
- `uv run pyright`: type-check `src` in strict mode.
- `uvx --from "git+https://github.com/overnightworks/quality@<sha>" quality check`: the gate's local check; `<sha>` is the one `.github/workflows/ci.yml` pins for quality's gate.
- `uv build`: build the wheel; run it after `uv sync`.
- `gh workflow run release.yml -f version=X.Y.Z`: dispatch a release; the pull request before it raises `version` in `pyproject.toml`.

## Rules

- The turn's vocabulary has one owner, `agents.turn`; `turn_events` is the in-process event, never a wire format.
- The libraries of the `backend` and `mcp-server` extras are imported only inside the module that needs them; `.importlinter` forbids them in the client's base modules.
- Deployment facts (the gateway origin and key file) come from the host's `AgentConfig`, never from an environment variable or a module default.
- Host obligations (the app's `HandTool`s, the MCP person front's handlers) are ports passed in; the library never invents them.
- Tests build their fakes with the builders in `tests/support`, not a second set.
- No host word appears in `src`, `tests` or `docs`; the "Host vocabulary" step in `.github/workflows/ci.yml` holds the list.
- A tag is never moved; a broken release gets the next patch tag.

## Owners

- The operator decides releases and dispatches them himself.

## Documents

- `README.md`: read when you need what the library is, how a host installs it and its extras.
- `docs/VISION.md`: read when you judge whether a change belongs in this library at all.
- `docs/ARCHITECTURE.md`: read when you change the shape of the client or must know why it was built so.
- `docs/calling.md`: read when you build or test an app's way for a person to call an agent to a place and send it away.
- `docs/hearing.md`: read when you build or test an app's way to let an agent hear what people say, or see, at a place.
- `docs/connector.md`: read when you change what an app sees: the turn, agent files, the tool contract, the gateway backend or the MCP person front.
