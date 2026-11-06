# Repository agent guidance

`agent-runtime` is the light client library `overnightworks-agent-runtime-client` (import name `agent_runtime_client`); every change here is a change to a public contract that a tag has already frozen elsewhere. Overview: README; decisions: `docs/decisions/`; breaking changes: `docs/MIGRATING.md`.

## Boundaries

`.importlinter` owns the layering and the forbidden imports; `uv run lint-imports`
proves it. The traps it cannot show:

- The run wire has one owner, `agent_runtime_client.wire`; the turn's vocabulary has one,
  `agent_runtime_client.turn`, which `wire` composes.
- `agent_runtime_client.turn_events` is the in-process turn event, never a second wire.
- The `backend` and `mcp-server` extras stay optional: their libraries are imported only inside the module that needs them.

## Code patterns

- Deployment facts come from the host, never from an environment variable or a
  module default. The gateway origin and key file arrive in the app's `agent`
  configuration section (`AgentConfig`); no path invents a default for a host fact.
- Host obligations are ports, passed in. The app's `HandTool`s and the MCP server's
  handlers arrive from the host; the library never invents them.
- Tests use the builders in `tests/support` (`wire_builders`, `gateway_turn_support`,
  `mcp_server_support`) rather than a second set of fakes.

## Checks

```bash
uv sync --locked --extra dev
uv run pytest -q
uv run ruff check src tests
uv run lint-imports
uv build
```

The suite needs no live provider. CI's "Host vocabulary" step greps
`src tests docs` for host words; `.github/host-vocabulary-allowed.txt`
owns the allowed hits. The required check is `quality / verdict`
(`quality/config.toml`, `.github/workflows/ci.yml`).

## Releasing

`main` is protected.

A release is dispatched, not pushed: `gh workflow run release.yml -f version=X.Y.Z -f commit=<main's head>`.
The head gives the operator that line with the version and commit filled in; only the operator runs it, as only he tagged. `.github/workflows/release.yml` gates first, then creates the tag at that commit through the API and attaches exactly
the one client wheel, built by the gate, to the GitHub release; hosts pin that wheel URL or the git tag (`@v<version>`), and never install by name from PyPI. A release whose `docs/MIGRATING.md`
holds `## Migrating to <version>` gets a release page that opens with a link to
that section, so a breaking release writes that section before the tag. A tag
is never moved — a broken release gets the next patch tag — so the pull
request that precedes the tag raises the one version literal, `version` in `pyproject.toml`.
