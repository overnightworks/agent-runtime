# App author guide

For the developer of an app that starts agent runs through the agent service:
the request an app sends, the events it reads back, the agent files it ships,
the tool contract it declares, the MCP endpoint it serves and what the service
keeps of a run. This is a
reference; `agent_runtime_client.wire` owns the wire (its stream rules sit in
`agent_runtime_client.run_stream` and `agent_runtime_client.sse`), `agent_runtime_client.agents` the agent
file, `agent_runtime_client.tools` the tool contract and `agent_runtime_client.mcp_server` the
tool endpoint, and their docstrings are exact where this file is short. The
decision record [0001](decisions/0001-agent-service.md) owns the rulings behind
them. Install the client as [host.md](host.md) describes; who may run what is the
operator's configuration ([operations.md](operations.md)).

## The run

`POST /runs` takes a `RunRequest` and answers with the run's event stream
(SSE), ending every run with exactly one terminal event except the cut stream
of a dropped client. A call carries the app's workload token in the `Agent-Workload-Token`
header, and the end user's token in `Authorization` when the run serves a person. A call with
neither `Authorization` nor `Agent-Job-Ticket` is the app's own run, admitted only for an agent
the operator enabled ([The app's own runs](#the-apps-own-runs)). `POST /runs` and
`GET /runs/{id}` answer a request carrying an `Agent-Job-Ticket` header, even an empty one, with
`401` before the body is read; the header's value is never read.

`RunRequest`, wire version 3:

| Field | Meaning |
|---|---|
| `v` | The protocol version, `3`. Any other body answers 400 `unsupported_version`. |
| `agent` | The agent's name, `[a-z][a-z0-9-]{0,63}`. It labels the run and its capability, and names the agent whose configured model the run uses; the request itself names no model, and a body that carries `model` answers 422 `invalid_request`. |
| `turn` | The system prompt and the messages (`Turn`, `TurnMessage`). |
| `roles` | The agent's allowed roles, exactly `["admin"]` or `["admin", "user"]`; `AgentDefinition.request` fills it from the agent file. |
| `tools` | A map from each tool the run may call to its `ToolEffect`; required, possibly `{}`. |

`GET /runs/{id}` returns the run's `RunStatus`, readable only by the app and
user that started it, or for the app's own run by that app with its workload token alone ([What a run keeps](#what-a-run-keeps) says what the record holds).

The stream's events are `run_started`, `assistant_text`, `tool_call`,
`tool_result`, `heartbeat` and one terminal event: `succeeded`, `failed`
(with a `FailureCode`) or `cancelled`. `run_started` carries `model` (the full
`<provider>/<model>`) and `route` (`cli` or `api`, a `RouteKind`), both required: the
names the service selected for the run, not proof of who answered. A run cut short after a side-effecting
tool call was emitted ends as `failed: interrupted`, not `cancelled`
(`ToolEffect`).

A request the service refuses answers a `RunRejection` with one `RejectionCode`:

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | A workload token is required, and a user token unless the app runs one of its own agents; every token sent must be valid. An `Agent-Job-Ticket` header on `POST /runs` or `GET /runs/{id}` is refused too. |
| `forbidden` | 403 | The user is no member of the app, the request's `roles` lack the caller's role, the request names a tool while the app has no MCP origin, the two tokens do not belong together (`agent_runtime_service.identity` lists every cause), or a run without a user token names an agent the app may not run as itself. |
| `not_enabled` | 403 | No route is enabled for this caller and model, the run names tools and the chosen subscription profile's provider cannot take tools yet. |
| `unsupported_version` | 400 | `v` is not the JSON integer 3. |
| `invalid_request` | 422 | The body is not a valid request for the route. |
| `capability_too_large` | 422 | The run's capability would exceed 8,192 characters. |
| `busy` | 429 | The subscription route's places are taken; `Retry-After` is set and the run never falls back to the paid route. |
| `unavailable` | 503 | The service cannot check tokens right now. |
| `not_found` | 404 | No such run, or not yours. |

## The app's own runs

A request with a valid workload token and neither `Authorization` nor `Agent-Job-Ticket` is the
app's own run. Selection is by header presence: any `Authorization` header, even an empty or
malformed one, is the person's path and answers its own errors, and the request never falls through
to the app's own run. The checks run in this order and the first failure answers:

1. The workload token is verified (401, or 503 when the key set is unreachable) and bound to its
   registered app as for a person's run (403).
2. Its `sub` is valid Unicode text, and its group claim, if present, is a list of strings (401).
3. The app has an agent with `runs_as_app = true` in `[apps.<app>.agents.<agent>]`, else 401
   before the body is read.
4. The body is checked as for a person's run, and its `agent` names such an agent, else 403
   `forbidden`. The body's `roles` must still be a valid list but is not read.
5. The route is decided by the service from the groups of the workload token and the model the
   configuration names for the agent, never named on the wire: no route group is 403 `not_enabled`, and a full subscription is
   429 `busy` without falling back.

The principal is the app's service account, `(iss, workload sub)`, and so is the run's owner with
the app: the capability names it as the principal like a person's, and `GET /runs/{id}` with the
workload token alone reads exactly the app's own runs.

The list of enabled agents is a tripwire for a cooperative app, not a boundary against a malicious
one: the request carries the whole turn and the agent is only a label. The boundary is the
Keycloak groups of the app's service account ([operations.md](operations.md#who-may-run-what)).

## Agent files

An app declares its agents as Claude Code subagent files,
`src/<pkg>/agents/<name>.md`, that `agent_runtime_client.agents.load_agent` reads into
an `AgentDefinition`: YAML front matter (`name`, `description`, `tools`, plus
the extension `appRoles`, exactly `[admin]`
or `[admin, user]`) and the Markdown body as the prompt. `AgentFileError` names
the file and the field. `offered_to(role)` is a display filter, not the
security boundary: the service checks the role.

## The tool contract

`agent_runtime_client.tools.ToolDeclaration` is the one declaration of a tool: name,
description, arguments and a `ToolEffect`, `read_only` or `side_effecting`. The
effect is for the service and never reaches a prompt; it defaults to
side-effecting, because a tool nobody classified must be assumed to change
state. The core's `ToolCatalog` builds on the same class.

## API text results

The API route buffers each complete gateway round before releasing its text or
tool calls. Calls run sequentially in provider order: a call starts only after
its event is accepted, and its result reaches the next gateway request only
after the result event is accepted. The final `succeeded` text concatenates the
rounds without trimming whitespace. Heartbeats continue while HTTP or tools wait,
without resetting the idle timeout. The configured model's full
`<provider>/<model>` name reaches the gateway unchanged across rounds, without fallback
or retry. The service's usual time, call and byte limits apply; there is no
additional eight-round limit.

Only requested and granted names present in the app's checked MCP offer reach
the gateway. A malformed or ungranted call refuses its whole round before text
or effects. Event budgets and recording remain per-event: a later refused call
never executes, but earlier accepted effects remain. A refused result never
reaches a further gateway request. Ordinary tool errors are returned to the model.
Malformed/incomplete replies end as `failed: provider_error`; HTTP refusals and
connection/TLS/read failures, or missing/invalid gateway credentials, end as
`failed: provider_unavailable`. Empty terminal text is refused; whitespace is preserved.

The turn, model, granted definitions and accepted results reach the gateway.
Only gateway Authorization carries the selected gateway credential; only app MCP
Authorization carries the capability. User/workload tokens, identity and
correlation metadata are not added; arbitrary app/user content is not anonymized.
Protected values in initial content, definitions or a decoded round are refused.
The accumulated assistant text is checked before releasing each round, so a
protected value completed across rounds refuses the completing round too.
The service's byte limit does not bound the buffered HTTP response.

## App tools through MCP

An app that offers tools declares one canonical https MCP origin in the
service's configuration. The app names the tools of each run in its request;
the service mints each admitted run a capability (an RS256 token) that grants exactly those
names and stamps the request's effects onto the run's tool calls (a tool
outside the map counts as side-effecting).

The app installs the client with `[mcp-server]` and serves
`McpServer.in_front_of(app)` as its outermost ASGI app, never a `Route` or
`Mount` in its router (the service refuses their slash redirects). It passes
the bytes of its public key (one PEM `PUBLIC KEY` block holding an RSA-3072 key
with exponent 65537), and each `McpTool` handler checks ownership with a fresh
database session. `verify_capability` returns the claims or one closed
`CapabilityRefusal` and never raises for token content; a body that carries
the capability's token or signature is replaced by an empty 500.
`agent_runtime_client.mcp_server` documents the order of the checks.

## What a run keeps

The service has no database. The messages, the tool calls and results, and the
answer pass through its memory only. For subscription runs, the service writes
the system prompt and, when the run offers tools, the tool relay's connection file
with its per-run secret into the run's private tree ([host.md](host.md), "A subscription run's
directories", owns the layout, the modes and when the tree is removed). What the
CLI may write, inside the tree or outside it, is under "What the CLI writes" below.
When the service cannot confirm that the run's process group is gone, it
quarantines the place: the whole tree stays, system prompt included, and the
service asks again every five seconds without a limit. If the service process ends
before the tree is removed, mid-run or in quarantine, the tree stays until the host
clears the working root.

**The run record.** The record behind `GET /runs/{id}` holds the run's owner (the
app and the person's token issuer and subject), its start and end timestamps, the
number of events, its terminal state and failure code, and whether the client was
lost. It holds no prompt, answer, tool input or result, failure message, token or
process group. It lives in memory only and no record survives a restart. A running
record never expires. An ended record answers a lookup for one hour after the run
ends; the service drops expired records from memory when it next handles a record,
so an idle service may hold one longer, but never returns it. At most 10,000 ended
records are kept; beyond that the oldest go first.

**The logs.** The service's own log lines carry classes, source positions, counts,
ids, process-group numbers and closed reason codes, never a prompt, an answer or CLI output; the CLI's
error stream is read and discarded. The HTTP server adds one access line per
request: the caller's address, the method, the path including its query string, the
protocol version and the status.

**Tokens.** The end user's token and the app's workload token are checked on the
request and none stored nor passed to the CLI. The service hands the CLI nothing
derived from them either: not the subject, issuer, groups, client id, app name or
run id, and not the capability, which it sends only to the app's MCP server. From
the service the CLI receives the app's turn, the model the configuration
names, the app's tool definitions and tool
results through the relay, the run's fixed flags and limits, the path of its system
prompt file and, when the run offers tools, of its relay file with the relay's
loopback address and per-run secret, and the service's own subscription credential;
[host.md](host.md), "The child environment is closed", lists the rest of its
environment. What the app writes is the app's: the definitions of the tools it
names, its turn and the text of its tool results can reach the provider, including
any value it takes from its capability's claims, such as the person's subject. The
service filters some of that content on the way, for example anything that carries
the capability verbatim, but it adds nothing of its own that identifies the person
or the app.

**What the CLI writes.** Every subscription run passes switches that limit what the
CLI keeps and sends ([host.md](host.md), "The child environment is closed").
[Decision record 0001](decisions/0001-agent-service.md) measured the CLI's own
writes with `--no-session-persistence` and without the telemetry switches: without
a login (A1b-0a c5), which also found an empty `/tmp/cc-socks-<uid>/` outside the
home, and with a login (A1b-0b1 c3), which lists the home only. A turn with the
run's full flags and switches in the real deployment, inside its home and outside
it, is not measured yet; issue #115, step 12b, owns that measurement.

**The provider.** The turn's text, the app's tool definitions, and the results of the
app's tools the run calls, reach the provider (Anthropic for Claude, OpenAI for Codex) under the
operator's subscription. What the provider keeps follows its terms, not this service.
