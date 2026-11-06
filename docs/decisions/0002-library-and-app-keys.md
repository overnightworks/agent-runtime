# 0002 — A library, and an app's own gateway key

- **Date:** 02.11.2026
- **Decided by:** the operator's ruling of 02.11.2026 on
  [#672](https://github.com/overnightworks/agent-runtime/issues/672): every app moves
  directly to LiteLLM, and agent-runtime ships the client (O1 a: each app holds its own
  LiteLLM key).
- **Amends:** the record 0001 of the agent service, replaced by [0003](0003-client-only.md).

## Decision

agent-runtime becomes a library. An app runs its agent turns through
`agent_runtime_client.backend.gateway.GatewayBackend`, which posts one chat completion to
the inference gateway with the app's own key. The service and the CLI route stop being the
way an app starts a run; the record of the service is gone with it (see [0003](0003-client-only.md)).

Each app holds its own LiteLLM key, given by the gateway's operator with its own budget and
limit. Model vendors stay reachable only through the gateway, so the key reaches nothing
else.

## Why

- A key per app gives each app its own budget and ceiling, which the service's one shared
  route could not.
- The client reaches the gateway with the one dependency its `backend` extra names and
  imports no core, web framework or login library.
- Rejected: wrapping the core's OpenAI adapter, which would give the client a dependency on
  the core; and a streamed round, which adds a parser no app needs, since a buffered reply
  is what apps already received.

## Consequences

- The key is a host fact: it arrives as a file path in the app's `agent` configuration
  section, is read per turn, and is written to no exception or log record.
- A person is known to the gateway only as a fingerprint of their issuer and subject, so a
  budget per person needs no real identifier to leave the app ([wire.md](../wire.md#the-app-connector)).
