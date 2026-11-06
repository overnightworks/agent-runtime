# 0003 — agent-runtime ships only the client

- **Date:** 06.11.2026
- **Decided by:** the operator's ruling of 06.11.2026 on
  [#697](https://github.com/overnightworks/agent-runtime/issues/697): remove what no app needs,
  because every app reaches its models through LiteLLM, and the record of the agent service
  goes with the service.
- **Replaces:** the record 0001, "One central agent service", which kept the service's
  rulings and is recoverable from git.

## Decision

The repository ships one distribution, `overnightworks-agent-runtime-client`, with the
import name `agent_runtime_client`. The agent service, the provider core and Grok are
deleted. Every app runs its agent turns through
`agent_runtime_client.backend.gateway.GatewayBackend` on its own LiteLLM key, and model
providers are reached only through LiteLLM.

What still holds of 0001, now owned by the client's code and [wire.md](../wire.md):

- The client is light. Its base depends on pydantic and ruamel.yaml; PyJWT with
  cryptography stay in the `mcp-server` extra and `httpx2` in the `backend` extra.
- `ToolDeclaration` names a tool with its JSON-schema input and an effect class,
  `read_only` or `side_effecting`. The class defaults to side-effecting and never reaches a
  model.
- An agent file is Claude Code's subagent format: YAML front matter between `---` lines, the
  Markdown body as the prompt. The client reads it strictly and validates each front-matter
  field by its own type: a limit must be an integer, `tools` and the roles are lists, and
  a quoted number is refused.
- The app owns its tools and their effects. They run in the app. The capability front (`McpServer`)
  accepts a call only with a capability that verifies against the public key the app holds; the
  person front (`McpPersonFront`) carries no capability, and the host's own authentication gate
  resolves the person before the front serves a call.

## Why

- No deployed app needs the core or the service once every app is on LiteLLM; their
  releases stay installable from the 0.21.0 release page.
- One distribution has one version literal, one wheel and no lockstep.
- Rejected: keeping the core as a library for hosts that run turns themselves, because no
  host does.

## Consequences

- A host drops `#subdirectory=packages/client` from a git pin
  ([MIGRATING](../MIGRATING.md#migrating-to-0220)).
- Each app holds its own LiteLLM key, given by the gateway's operator with its own budget
  and limit ([0002](0002-library-and-app-keys.md)).
