# Repository agent guidance

`agent-runtime` is the client library `overnightworks-agent-runtime-client` (import name
`agent_runtime_client`); every change here is a change to a public contract that a tag has
already frozen elsewhere. Overview: README; reasons and what was not chosen: docs/ARCHITECTURE.md.

## Boundaries

`.importlinter` owns the layering and the forbidden imports; the gate's `import-contract` rule
proves it (`quality check`). The traps it cannot show:

- The turn's vocabulary has one owner, `agent_runtime_client.turn`; `turn_events` is the
  in-process event, never a wire format.
- The `backend` and `mcp-server` extras stay optional: their libraries are imported only inside
  the module that needs them.

## Code patterns

- Deployment facts come from the host, never from an environment variable or a module default.
  The gateway origin and key file arrive in the app's `agent` configuration section
  (`AgentConfig`); no path invents a default for a host fact.
- Host obligations are ports, passed in. The app's `HandTool`s and the MCP person front's
  handlers arrive from the host; the library never invents them.
- Tests use the builders in `tests/support` rather than a second set of fakes.

## Checks

```bash
uv sync --locked --extra dev
uv run pytest -q
uv run ruff check src tests
uvx --from "git+https://github.com/overnightworks/quality@2d2b68bf3916b34f6f2c72a540c1e3d8a947b750" quality check
uv build
```

The suite needs no live provider. CI's "Host vocabulary" step greps `src tests docs` for host
words and fails on any hit. The required check is
`quality / verdict` (`quality/config.toml`, `.github/workflows/ci.yml`).

## Releasing

`main` is protected.

A release is dispatched, not pushed: `gh workflow run release.yml -f version=X.Y.Z`.
The head gives the operator that line with the version filled in; only the operator runs it, as
only he tagged. `release.yml` calls quality's `release-gate`, which judges main's head at the
dispatch; it then builds the one client wheel at that commit and calls quality's `release-tag`,
which creates the tag there and attaches exactly that wheel to the GitHub release. Hosts pin
the git tag (`@v<version>`) and never install by name from PyPI. A tag is never moved: a broken
release gets the next patch tag, so the pull request that precedes the tag raises the one version
literal, `version` in `pyproject.toml`.
