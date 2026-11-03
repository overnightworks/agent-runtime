# App author guide

For the developer of an app that starts agent runs through the agent service:
the request an app sends, the events it reads back, the agent files it ships,
the tool contract it declares, the MCP endpoint it serves and what the service
keeps of a run. This is a
reference; `agent_runtime_client.wire` owns the wire (its stream rules sit in
`agent_runtime_client.run_stream` and `agent_runtime_client.sse`), `agent_runtime_client.turn` the
turn's vocabulary that the wire composes, `agent_runtime_client.agents` the agent
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

`RunRequest`, wire version 6:

| Field | Meaning |
|---|---|
| `v` | The protocol version, `6`. Any other body answers 400 `unsupported_version`. |
| `agent` | The agent's name, `[a-z][a-z0-9-]{0,63}`. It labels the run and its capability, and names the agent whose configured model the run uses unless the request names a model. |
| `turn` | The system prompt and the messages (`Turn`, `TurnMessage`). |
| `roles` | The agent's allowed roles, exactly `["admin"]` or `["admin", "user"]`; a host builds the `RunRequest` and sets it. |
| `tools` | A map from each tool the run may call to its `ToolEffect`; required, possibly `{}`. |
| `model` | Optional. A full `<provider>/<model>` name (no version) the installation offers: the full names in its `[cli.<provider>] models` lists, the same for every app. A named model wins over the agent's, the app's and the default; absent or `null` runs the configured model as before. A name outside the grammar answers 422 `invalid_request`, a valid name the installation does not offer 422 `model_not_offered`, checked after the `forbidden` refusals and before the route. The host sets it; `AgentDefinition.request()` returns a `TurnRequest`, not a `RunRequest`. |

`GET /runs/{id}` returns the run's `RunStatus`, readable only by the app and
user that started it, or for the app's own run by that app with its workload token alone ([What a run keeps](#what-a-run-keeps) says what the record holds).

`GET /models` returns the `ModelList` `{"v": 6, "object": "list", "data": [{"id": "<provider>/<model>", "object": "model"}]}`:
exactly the models a run may name (`ServiceConfig.offered_models()`), sorted by id, `Cache-Control:
no-store`. The workload token of any registered app reads it alone, whether or not it runs an
agent as itself; `Authorization` is never read. It derives from the configuration only, so every app
and every instance of one configuration answers the same bytes, whatever runs are in flight.
An `Agent-Job-Ticket` header is `401` before any token is read; the other refusals are `RunRejection`
bodies: `401` for a missing or invalid workload token, `503` with `Retry-After` for an unreachable
key set, `403` for a token bound to no registered app. Every listed name is admitted as a run's `model`.

The stream's events are `run_started`, `assistant_text`, `tool_call`,
`tool_result`, `heartbeat` and one terminal event: `succeeded`, `failed`
(with a `FailureCode`) or `cancelled`. `run_started` carries `model` (the full
`<provider>/<model>`) and `route` (`cli` or `api`, a `RouteKind`), both required: the
names the service selected for the run, not proof of who answered. A run cut short after a side-effecting
tool call was emitted ends as `failed: interrupted`, not `cancelled`
(`ToolEffect`).

`succeeded` carries `text` and `files`, and `files` is always present, possibly `[]`. A file is
`{"name": "<name>", "content": "<base64>"}`: its bytes and nothing else, with no media type.
A name is one path component of 1 to 255 bytes in UTF-8, holds no `/` and no NUL, and is not `.` or `..`;
names are unique within a run. `content` is standard base64 with padding (RFC 4648 section 4) in its one
canonical spelling: whitespace, the URL-safe alphabet, missing padding and non-zero pad bits are refused,
and `""` is a file of zero bytes. All contents together hold at most `MAX_RUN_FILES_BYTES`, 32 MiB.
`text` may be empty only when `files` holds a file; a run without files must have words.

A frame holds at most `MAX_EVENT_BYTES` (1 MiB), except a `succeeded` frame, which holds at most
`MAX_SUCCEEDED_FRAME_BYTES` (`MAX_EVENT_BYTES` plus the base64 length of 32 MiB, 45,787,820 bytes), and
whose bytes without its files' base64 still hold at most `MAX_EVENT_BYTES`. Base64 pads each file
separately, so files that sum to 32 MiB can still exceed the frame ceiling. The decoder gives the larger
allowance only to a frame whose `event` line says `succeeded` before its `data` line. An oversized frame
is `oversized_frame` in the decoder and `EventTooLarge` in the encoder.

A request the service refuses answers a `RunRejection` with one `RejectionCode`:

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | On `POST /runs` and `GET /runs/{id}`: a workload token is required, and a user token unless the app runs one of its own agents; every token sent must be valid and carry a readable `sub` and group claim. On `GET /models` only the workload token is read, and a missing or invalid one is refused. An `Agent-Job-Ticket` header on any of these routes is refused too. |
| `forbidden` | 403 | The user is no member of the app, the request's `roles` lack the caller's role, the request names a tool while the app has no MCP origin, the two tokens do not belong together ([webauth's host contract](https://github.com/overnightworks/webauth/blob/v0.29.0/docs/host-contract.md#a-services-callers) lists the causes of both codes in the order they are checked: every 401 and 503 about the tokens comes before any 403), or a run without a user token names an agent the app may not run as itself. |
| `not_enabled` | 403 | No route is enabled for this caller and model. |
| `unsupported_version` | 400 | `v` is not the JSON integer 6. |
| `model_not_offered` | 422 | The request names a model in the grammar that the installation does not offer. A run that fails keeps the model it named; nothing falls back to another. |
| `invalid_request` | 422 | The body is not a valid request for the route. |
| `too_large` | 413 | The body is over 262,144 bytes (`MAX_REQUEST_BYTES`), whether its length is declared or streamed. Checked after the tokens and the caller check, before the body is parsed; a declared length over the limit is not read. A `Content-Length` that is not a number answers `invalid_request`; behind uvicorn, uvicorn itself answers it with a bare 400 before the application sees the request. |
| `capability_too_large` | 422 | The run's capability would exceed 8,192 characters. |
| `busy` | 429 | The subscription route's places are taken; `Retry-After` is set and the run never falls back to the paid route. |
| `unavailable` | 503 | The service cannot check tokens right now. |
| `not_found` | 404 | No such run, or not yours. |

## The app's own runs

A request with a valid workload token and neither `Authorization` nor `Agent-Job-Ticket` is the
app's own run. Selection is by header presence: any `Authorization` header, even an empty or
malformed one, is the person's path and answers its own errors, and the request never falls through
to the app's own run. The checks run in this order and the first failure answers:

1. The workload token is verified (401, or 503 when the key set is unreachable), its `sub` is valid
   Unicode text and present, and its group claim, if present, is a list of strings (401).
2. It is bound to its registered app as for a person's run (403).
3. The app has an agent with `runs_as_app = true` in `[apps.<app>.agents.<agent>]`, else 401
   before the body is read.
4. The body is checked as for a person's run, and its `agent` names such an agent, else 403
   `forbidden`. The body's `roles` must still be a valid list but is not read.
5. The route is decided by the service from the groups of the workload token and the run's model
   (the body's `model` if it names one the installation offers, else the configuration's for the agent): no route group is 403 `not_enabled`, and a full subscription is
   429 `busy` without falling back.

The principal is the app's service account, `(iss, workload sub)`, and so is the run's owner with
the app: the capability names it as the principal like a person's, and `GET /runs/{id}` with the
workload token alone reads exactly the app's own runs.

The list of enabled agents is a tripwire for a cooperative app, not a boundary against a malicious
one: the request carries the whole turn and the agent is only a label. The boundary is the
Keycloak groups of the app's service account ([operations.md](operations.md#who-may-run-what)).

## The app connector

An app runs a turn through `agent_runtime_client.backend.AgentBackend`, installed as
`src/<app>/agent/backend.py` with its tools in `src/<app>/agent/tools.py`.
`backend.run(request, person)` is an async generator of `TurnEvent`s that the app consumes inside
`contextlib.aclosing`; closing it runs the backend's cleanup. The package's base modules import no HTTP
library; the one implementation, `backend.gateway.GatewayBackend`, needs the `backend` extra.

`request` is a `TurnRequest` (`agent_runtime_client.agents`, frozen): `agent`, `turn`, `roles`,
`tools` (the agent's selected `ToolDeclaration`s), `model`, `max_duration_seconds`, `max_tool_calls`
(`None` exactly when `tools` is empty) and `picture_size` (`None` for a text agent, see A picture agent).
`AgentDefinition.request(messages, *, model=None)` builds it; `model` overrides the file's model
for that run, a model of the agent's kind.

`person` is a `TurnPerson` (frozen, always a person): `principal` (the verified `(iss, sub)`),
`role` (the app role) and `groups`, an async callable returning the person's groups.

A turn ends short of its answer with one exception, `TurnStopped(kind, code, retry_after_s,
after_side_effect, detail)`: `refused` carries a `RejectionCode`, `failed` a `FailureCode`,
`cancelled` none. `after_side_effect` is true once a side-effecting tool call started in the turn,
whatever ended it, `refused: busy` included, so an app does not retry a turn that may have
written.

The app's configuration embeds `agent_runtime_client.config.AgentConfig` as the `agent` section:
`backend` (`litellm`, the one value), `gateway` and `api_group`, none with a default.
`ChatGateway` is `origin` (scheme, host and optional port; no path, query, fragment or userinfo),
`key_file` and `ca_file`, both absolute paths. `ca_file` is required for HTTPS and forbidden for
HTTP.

### What every app does the same

Every app names the same files: `agent/backend.py` builds the backend, `agent/tools.py` holds
its tools, `agent/mcp.py` its MCP surface, and `agent/settings.py` its `agent` configuration
section. `agent/settings.py` is the one place that imports `agent_runtime_client.config`.
import-linter folds an external package into one graph node, so an app without marked routes
can forbid the client outside `<app>.agent` outright; an app with marked routes holds the
`.tools`-only rule by review.

`TurnPerson` is always the verified person, and `principal.iss` is passed exactly as the app
bound it: two spellings of one issuer split one person's budget.

`busy` may carry no `retry_after_s`, so the app needs a defined fallback; a value that varies
by app stays the app's.

Tests use a fake `AgentBackend` that yields `TurnEvent`s and raises `TurnStopped`. A proof
through the real entry point runs `GatewayBackend` on `backend.fake_gateway.FakeGateway`, a
scripted transport passed as `transport=`.

### The gateway backend

`GatewayBackend(config, *, tools=(), transport=None)` runs a text turn, with the app's
`HandTool`s when the agent selects any (a tool name given twice raises `ValueError` at construction), or a picture agent's turn (below). A selected tool with no handler, or whose request
declaration differs from its `HandTool`'s, raises `ValueError` at `run`, before any request. In order, before any request: a `person.role`
outside `request.roles` is `refused: forbidden`; `person.groups()` runs once and a refusal it
raises reaches the app unchanged; an `api_group` the person lacks is `refused: not_enabled`.
The key file is read per turn after that: one printable ASCII line of at most 1024 bytes,
opaque whatever its prefix; any other content is `failed: provider_unavailable`.
`request.max_duration_seconds` is one deadline around the whole turn, every round and handler
included; past it the turn `failed: limit_exceeded` and a running handler is cancelled.

What the client sends for a text turn, and nothing else:

| Part | Value |
| --- | --- |
| Request | `POST <origin>/v1/chat/completions`, no redirect followed, no cookie kept, no proxy variable read; HTTPS trusts `ca_file` alone |
| Headers | `Authorization: Bearer <key>`, `Content-Type: application/json`, and the transport's own defaults; no `x-litellm-*` or `anthropic-*` header, and nothing relies on `User-Agent` or `x-litellm-tags` |
| Body | exactly `model` (the request's `text/<model>` name), `messages` (the system prompt, then the turn's messages, then the round's calls and results), `user` and, when the agent has tools, `tools` (OpenAI function tools of each declaration's `name`, `description` and `parameters`; `effect` never reaches the model); no `stream`, `max_tokens`, `metadata`, `tags`, `fallbacks`, `num_retries`, `mock_response` or `api_key` |
| `user` | `"fp-" + sha256(json.dumps([iss, sub], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()`, 67 characters |

The fingerprint's prefix, input and algorithm are one contract across apps: a change splits
every person's budget. `["https://issuer.example", "subject-1"]` gives
`fp-2db44d96ffdef38f9e1cbfe73e9cf248fb66955b894846b34843a2d3a849d764`, and a non-ASCII subject, `["https://issuer.example", "subjéct-ü-1"]`, gives
`fp-4670d575ad432d6edf969bf640bca6a2114230ad288ca5893725c511dd7c94c9`.

| The gateway answers | The turn ends |
| --- | --- |
| 200, `finish_reason: stop`, non-empty text of at most `MAX_EVENT_BYTES` | one `AssistantTextEvent`, then `FinalEvent` |
| 200, `finish_reason: tool_calls`, whole round | the calls run in order (see below), then the next request |
| 200 otherwise (malformed JSON, no choices, empty text, another `finish_reason`, a lone surrogate, a longer reply, a round that is not whole) | `failed: provider_error` |
| 429 | `refused: busy`; `retry_after_s` from the `Retry-After` seconds, at most `MAX_RETRY_AFTER_S`; an HTTP-date or no header gives none |
| another 4xx, a budget refusal included | `failed: provider_unavailable`, detail `gateway_refused`, body unread |
| 1xx, a 2xx other than 200, 3xx (a redirect is not followed) or 5xx (a `Retry-After` is ignored) | `failed: provider_unavailable`, detail `gateway_failed` |
| connection refused or reset, TLS failure, an origin the HTTP library refuses | `failed: provider_unavailable` |

A person over budget therefore reads as "unavailable", the same as any other refusal; telling
them apart is a named gap. `detail` is for logs only.

#### The tool loop

The loop runs in the client, in process, over buffered rounds. A round is judged whole before
anything shows or runs: only the request's tool names are offered and accepted, so a call to
any other name, arguments that are not a JSON object, an empty round or a call id used twice in
the turn refuses the round (`failed: provider_error`) with no handler run. A round that would
take the turn past `max_tool_calls` ends `failed: limit_exceeded` the same way.

For each call, in order: `ToolCallEvent`; the side-effect state set from the declaration's
`ToolEffect`, before the handler runs; `await handler(principal, arguments)`;
`ToolResultEvent`. A call, or the next request, that the deadline has already passed when its
turn comes (a slow consumer of the events) is not started: `failed: limit_exceeded`, with the
side-effect state unchanged by it. An outcome that does not make a valid `ToolResultEvent` (a `content`
that is not text, an `is_error` that is not a bool), or whose content a UTF-8 body cannot carry, is a
host defect like a handler that raises. The model reads an error outcome as `Error: ` followed by the content, because the chat format has
no error flag on a tool message. A call that started may have written, and a cancelled `asyncio.to_thread`
write may still finish; nothing implies it was undone. A blocking tool stays the app's: its
handler uses `asyncio.to_thread`. A `ToolOutcome(is_error=True)` goes to the model and the turn
goes on. A handler that raises is a host defect: the client logs its class name only and tells
the model the call failed. Task cancellation reaches the caller unchanged and no later round
runs. Per started call one INFO record carries the tool name, call id and `is_error`, never the
arguments or the content; a call the deadline or a cancellation cut off leaves its record too, with
`is_error` unset. A call refused before its handler started ran nothing and leaves no record.

A tool's `content` reaches the model, and so the gateway and the provider the model names:
a tool returns only what that may see.

| Bound | Value | Over it |
| --- | --- | --- |
| One tool result | `MAX_EVENT_BYTES` | the model gets a fixed `is_error` sentence instead of the content; the side effect stands; the turn continues |
| One request of a turn, serialised | 4 × `MAX_EVENT_BYTES` | no request is sent; `failed: limit_exceeded`. It bounds a text turn's first request too: a turn input near that size ends `failed: limit_exceeded` |
| One response body | as for a text turn | `failed: provider_error` |

#### A picture agent

A request with a `picture_size` (an agent file's `picture: {size: <width>x<height>}`) runs after
the same admission and under the same one deadline, as one call: `POST <origin>/v1/images/generations`
with a text turn's headers, client and request bound. The body is exactly `model` (the request's
`image/<model>` name), `prompt` (the agent's prompt, a blank line, the message), `size` and `user`
(the fingerprint above, so one person has one budget in text and pictures); `n` and
`response_format` are never sent. A picture request holds exactly one user message and no tools,
refused when the `TurnRequest` is built.

The reply holds exactly one `data` entry with `b64_json`. A non-null `url` is refused and never
fetched; `url: null` beside `b64_json` passes. The turn ends with
`FinalEvent(text="", files=(RunFile(name="picture", content=<the bytes>),))` and no
`AssistantTextEvent`. No media type travels: `RunFile` holds none, and the app sniffs the bytes.

| Bound or reply | Value | Over it or otherwise |
| --- | --- | --- |
| The HTTP body | `base64_length(MAX_RUN_FILES_BYTES)` plus `MAX_EVENT_BYTES` for the JSON around it | `failed: provider_error`, as a text body over its bound |
| The decoded picture | canonical base64 by `RunFile`'s reader, at most `MAX_RUN_FILES_BYTES` (the client checks it; `RunFile` holds no ceiling) | `failed: provider_error` |
| Malformed JSON, a non-null `url`, no entry or more than one, bad, empty or non-canonical base64 | | `failed: provider_error` |
| 429, another status, a connection or read failure, the deadline | | as for a text turn: `refused: busy`, `failed: provider_unavailable`, `failed: limit_exceeded` |

A vendor's moderation refusal is a 400 and so reads as `provider_unavailable`; telling it apart
is a named gap, and an app shows that it did not create a picture. Closing the stream cancels the
HTTP call, and the vendor may still bill it. Which sizes exist is the gateway's: the client
checks the size's shape only.

#### The model list

`await list_gateway_models(config.gateway, *, transport=None)` (in `agent_runtime_client.backend.gateway`) answers a `GatewayModels`
for an admin page, which has no turn. It sends a bare `GET <origin>/v1/models` with a text turn's client rules and
`Authorization: Bearer <key>` alone: no body, no query and no other header of its own. The key file is read per call.
The whole call has one deadline of 10 seconds and a reply body bound of `MAX_EVENT_BYTES`.

`GatewayModels.text` and `.image` hold the `data[].id` names of each kind in the gateway's order; an id that is no
`text/…` or `image/…` model name is not offered and is no error. `for_agent(agent)` answers the names of that
`AgentDefinition`'s kind, the ones its `request(messages, model=…)` accepts.

Every failure raises the one `ModelListUnavailable`, whose `detail` (`key_unusable`, `gateway_refused`,
`gateway_failed`, `connection_failed`, `malformed_reply` or `timed_out`) is a closed label; neither the key nor any text
of the reply is in it, and the key is in neither the exception's cause nor its traceback's frames. That covers an unusable key file, a 4xx (a 429 too), a 5xx or redirect, a lost connection or an origin the HTTP library refuses, the
deadline, and a body that is over its bound or is not a JSON object whose `data` is a list of objects with a text `id`.

An app sets an agent's model with `agent.request(messages, model=name)`; without `model` the file's model runs. A name
of the wrong kind (a picture agent needs `image/…`, any other agent `text/…`) raises `ValueError` before any request.
The gateway may later withdraw a name an app stored: that turn ends `failed: provider_unavailable` and the client does
not fall back to the file's model.

`GatewayBackend`'s constructor and every `list_gateway_models` call set the `httpx2` and `httpcore2` loggers to WARNING for the whole process, overriding an app's own DEBUG setting there.

## Agent files

An app declares its agents as Claude Code subagent files,
`src/<pkg>/agents/<name>.md`, that `agent_runtime_client.agents.load_agent` reads into
an `AgentDefinition`: YAML front matter (`name`, `description`, `tools`, `model`,
`maxDurationSeconds`, `maxToolCalls`, `picture`, plus the extension `appRoles`, exactly `[admin]`
or `[admin, user]`) and the Markdown body as the prompt. `model` is a `text/<model>` name for a text agent
(lower-case letters, digits, `.` and `-`) and `maxDurationSeconds` a positive integer; both are
required. `picture: {size: <width>x<height>}` (positive whole numbers, nothing else in the mapping) makes a picture agent: its `model` must be an `image/<model>` name, and it selects no `tools` and carries no `maxToolCalls`; an agent without `picture` must name a `text/…` model. `maxToolCalls` is a positive integer, required exactly when `tools` is not empty and refused when it is. `AgentFileError` names
the file and the field. `offered_to(role)` is a display filter, not the
security boundary.

## The tool contract

`agent_runtime_client.tools.ToolDeclaration` is the one declaration of a tool: name,
description, arguments and a `ToolEffect`, `read_only` or `side_effecting`. The
effect is for the host (the service and `GatewayBackend` read it for `after_side_effect`) and
never reaches a prompt; it defaults to side-effecting, because a tool nobody classified must be assumed to change
state. The core's `ToolCatalog` builds on the same class.

`HandTool` is a declaration with the async handler `(Principal, JsonObject) -> ToolOutcome`
that serves it in the app's own process; `ToolOutcome` is `content` and `is_error`. The module
exports `Principal` and `JsonObject` too, so an app's `agent/tools.py` imports only
`agent_runtime_client.tools`. It imports neither `wire` nor `mcp_wire`, which binds
`ToolOutcome` for its own use but does not export it to a type checker.

## API text results

The API route buffers each complete gateway round before releasing its text or
tool calls. Calls run sequentially in provider order: a call starts only after
its event is accepted, and its result reaches the next gateway request only
after the result event is accepted. The final `succeeded` text concatenates the
rounds without trimming whitespace. Heartbeats continue while HTTP or tools wait,
without resetting the idle timeout. The run's model's full
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

## The person front

`agent_runtime_client.mcp_server.McpPersonFront(tools, *, resolve_person)` is the
MCP endpoint of an app for the agent of one signed-in person. It is an ASGI 3 app
the host registers as the exact route `MCP_ENDPOINT_PATH`, never a `Mount`, which
redirects `/mcp` to `/mcp/`, and behind the middleware that admits the caller:
webauth's `BearerGate(resource=…)` answers every 401 and 403 before routing, so the
front answers no challenge.

`resolve_person` is a port, `Callable[[Scope], Principal]`, and the front's one seam
to webauth: the client imports no webauth, and the host binds the port to
`webauth.middleware.resource_person`. The front calls it first on every request,
before it reads the body. Its failure propagates to the host: no tool runs and the
front writes no answer, because a front reached without the gate's admission is a
host defect.

A tool is `PersonTool(declaration, handler)`; the handler is
`PersonToolHandler = Callable[[PersonRequest, JsonObject], Awaitable[ToolOutcome]]`,
and `PersonRequest(principal, scope)` carries the resolved `Principal` and the
request's ASGI scope, which holds the bearer, `Host` and client address a handler
replays. A name given twice raises `ValueError` at construction.

A request with an `Origin` header is 403 (its clients are agents, which send none),
anything but `POST` is 405 with `Allow: POST`, and a body over `MAX_EVENT_BYTES` is
413. The profile is the capability front's: `mcp_wire.decide_server_message`
decides. `tools/list` offers every tool, because rights are the route's, with
`annotations.readOnlyHint` true for a `READ_ONLY` tool and absent otherwise. A
handler that raises answers a failed tool result with one fixed text; the log
names the tool and the exception's class, and nothing of its message reaches the
wire or the log.

## What a run keeps

The service has no database. The messages, the tool calls and results, the
answer and the files a run returns pass through its memory only. The core attempts to
remove the run's tree, `output/` included, before the frame is built; a removal that fails or
is refused is logged and does not stop the frame ([host.md](host.md) owns the order and the
cleanup-failure policy). For subscription runs, the service writes
the system prompt and, when a Claude run offers tools, the tool relay's connection file
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
the service the CLI receives the app's turn, the model the run
selected, the app's tool definitions and tool
results through the relay, the run's fixed flags and limits, the path of its system
prompt file and, when the run offers tools, the relay's loopback address and
per-run secret (a Claude run gets both in its relay file; a Codex or Grok run gets
the address in its `config.toml` and the secret only in its environment), and the
service's own subscription credential;
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

**The provider.** On the subscription route, the turn's text reaches the provider
(Anthropic for Claude, OpenAI for Codex, xAI for Grok) under the operator's
subscription, and the app's tool definitions and the results of the app's tools reach
Anthropic on a Claude run, OpenAI on a Codex run and xAI on a Grok run. On the API route, the turn, the model, granted definitions
and accepted results reach the gateway, which forwards them to the provider the model
names ([API text results](#api-text-results)). What the gateway and the provider keep
follows their terms, not this service.
