# Connector reference

For the developer of an app that runs agent turns through `agent_runtime_client`: the turn an
app starts, the agent files it ships, the tools it declares, the gateway backend and the MCP
person front with its wait tool. Each rule names the test that proves it. The docstrings of
`agents`, `tools`, `turn`, `wait`, `backend` and `mcp_server` are exact where this file is
short; the shape of the client is in [ARCHITECTURE.md](ARCHITECTURE.md).

## The turn

An app runs a turn through `agent_runtime_client.backend.AgentBackend`, installed as
`src/<app>/agent/backend.py` with its tools in `src/<app>/agent/tools.py`.
`backend.run(request, person)` is an async generator of `TurnEvent`s that the app consumes inside
`contextlib.aclosing`; closing it runs the backend's cleanup. The base modules import no HTTP
library; the one implementation, `backend.gateway.GatewayBackend`, needs the `backend` extra.

`request` is a `TurnRequest` (`agent_runtime_client.agents`, frozen): `agent`, `turn`, `roles`,
`tools` (the agent's selected `ToolDeclaration`s), `model`, `max_duration_seconds`,
`max_tool_calls` (`None` exactly when `tools` is empty) and `picture_size` (`None` for a text
agent). `AgentDefinition.request(messages, *, model=None)` builds it; `model` overrides the
file's model for that run, with a model of the agent's kind.
Proved by `tests/test_agents_file.py`: `test_request_carries_the_files_prompt_model_roles_and_tools`,
`test_request_model_is_the_override_when_given`, `test_request_is_frozen`.

`person` is a `TurnPerson` (frozen, always a person): `principal` (the verified `(iss, sub)`),
`role` (the app role) and `groups`, an async callable returning the person's groups. Proved by
`tests/test_backend_port.py`: `test_a_turn_person_is_not_constructible_without_a_principal`.

A turn ends short of its answer with one exception, `TurnStopped(kind, code, retry_after_s,
after_side_effect, detail)`: `refused` carries a `RejectionCode`, `failed` a `FailureCode`. A cancel
reaches the caller as `CancelledError`. `after_side_effect` is true once a side-effecting tool call
started in the turn, whatever ended it, `refused: busy` included, so an app does not retry a turn
that may have written. Proved by `tests/test_backend_gateway_tools.py`:
`test_a_busy_gateway_at_round_two_reports_whether_a_call_may_have_written`.

The app's configuration embeds `agent_runtime_client.config.AgentConfig` as the `agent` section:
`backend` (`litellm`, the one value), `gateway` and `api_group`, none with a default.
`ChatGateway` is `origin` (scheme, host and optional port; no path, query, fragment or
userinfo), `key_file` and `ca_file`, both absolute paths. `ca_file` is required for HTTPS and
forbidden for HTTP. A missing or malformed value fails loud. Proved by
`tests/test_backend_config.py`: `test_a_missing_host_fact_fails_loud`,
`test_a_malformed_origin_fails_loud`, `test_a_malformed_file_setting_fails_loud`.

### What every app does the same

Every app names the same files: `agent/backend.py` builds the backend, `agent/tools.py` holds
its tools, `agent/mcp.py` its MCP surface, and `agent/settings.py` its `agent` configuration
section. `agent/settings.py` is the one place that imports `agent_runtime_client.config`.

`TurnPerson` is always the verified person, and `principal.iss` is passed exactly as the app
bound it: two spellings of one issuer split one person's budget. `busy` may carry no
`retry_after_s`, so the app needs a defined fallback; a value that varies by app stays the
app's.

Tests use a fake `AgentBackend` that yields `TurnEvent`s and raises `TurnStopped`. A proof
through the real entry point runs `GatewayBackend` on `backend.fake_gateway.FakeGateway`, a
scripted transport passed as `transport=`.

## Agent files

An app declares its agents as Claude Code subagent files, `src/<pkg>/agents/<name>.md`, that
`agent_runtime_client.agents.load_agent` reads into an `AgentDefinition`: YAML front matter
(`name`, `description`, `tools`, `model`, `maxDurationSeconds`, `maxToolCalls`, `picture`, plus
the extension `appRoles`, exactly `[admin]` or `[admin, user]`) and the Markdown body as the
prompt.

- `model` is a `text/<model>` name for a text agent (lower-case letters, digits, `.` and `-`)
  and `maxDurationSeconds` a positive integer; both are required. Proved by
  `tests/test_agents_file.py`: `test_a_file_without_a_required_key_is_refused_and_names_it`,
  `test_a_model_that_is_not_a_text_gateway_name_is_refused_and_named`.
- `maxToolCalls` is a positive integer, required exactly when `tools` is not empty and refused
  when it is. Proved by `test_a_missing_tool_call_bound_is_refused_when_there_are_tools` and
  `test_a_tool_call_bound_is_refused_when_there_are_no_tools` in the same file.
- `picture: {size: <width>x<height>}` (positive whole numbers, nothing else in the mapping)
  makes a picture agent: its `model` must be an `image/<model>` name, and it selects no `tools`
  and carries no `maxToolCalls`. Proved by
  `test_a_picture_file_that_does_not_fit_a_picture_agent_is_refused_and_named`.
- A tool the file names must be declared, once. Proved by `test_undeclared_tool_is_refused_and_named`
  and `test_duplicate_tool_in_the_file_is_refused`.
- The file is read strictly: an unknown or duplicate key, malformed front matter, an unquoted
  type mismatch and a name that differs from the file's stem are refused with the file and the
  field named, never the value. Proved by `test_unknown_key_is_refused_and_named`,
  `test_duplicate_key_is_refused_and_named` and
  `test_formatted_traceback_does_not_show_file_values`.
- `offered_to(role)` is a display filter, not the security boundary. Proved by
  `test_offered_to_follows_the_app_roles`.

## The tool contract

`agent_runtime_client.tools.ToolDeclaration` is the one declaration of a tool: name,
description, arguments and a `ToolEffect`, `read_only` or `side_effecting`. The effect is for the
host (`GatewayBackend` reads it for `after_side_effect`) and never reaches a prompt; it defaults
to side-effecting, because a tool nobody classified must be assumed to change state. Proved by
`test_the_request_carries_only_model_messages_user_and_tools_without_the_effect` in
`tests/test_backend_gateway_tools.py`.

`HandTool` is a declaration with the async handler `(Principal, JsonObject) -> ToolOutcome` that
serves it in the app's own process; `ToolOutcome` is `content` and `is_error`. The module
exports `Principal` and `JsonObject` too, so an app's `agent/tools.py` imports only
`agent_runtime_client.tools`. It imports neither `mcp_wire` nor anything of the MCP extra
(`.importlinter` proves it).

## The gateway backend

`GatewayBackend(config, *, tools=(), transport=None)` runs a text turn, with the app's
`HandTool`s when the agent selects any, or a picture agent's turn. A tool name given twice
raises `ValueError` at construction; a selected tool with no handler, or whose request
declaration differs from its `HandTool`'s, raises `ValueError` at `run`, before any request.
Proved by `tests/test_backend_gateway_tools.py`: `test_two_tools_with_one_name_are_refused_at_construction`,
`test_a_selected_tool_without_a_handler_raises_before_any_request`,
`test_a_request_declaration_that_differs_from_its_handler_s_raises_before_any_request`.

**Admission,** in order, before any request: a `person.role` outside `request.roles` is
`refused: forbidden`; `person.groups()` runs once and a refusal it raises reaches the app
unchanged; an `api_group` the person lacks is `refused: not_enabled`. Proved by
`tests/test_backend_gateway.py`: `test_a_role_outside_the_agents_roles_is_forbidden_and_sends_nothing`,
`test_a_group_the_person_lacks_is_not_enabled_and_sends_nothing`,
`test_a_refusal_the_groups_lookup_raises_reaches_the_app_unchanged`,
`test_groups_are_asked_once_per_turn`.

**The key** is read per turn after admission: one printable ASCII line of at most 1024 bytes,
opaque whatever its prefix; any other content is `failed: provider_unavailable`. Proved by
`test_an_unusable_key_file_is_provider_unavailable_and_sends_nothing` and
`test_a_key_with_any_prefix_is_sent_as_it_is`. No log record holds it
(`test_debug_logging_writes_the_key_into_no_record`).

**The deadline.** `request.max_duration_seconds` is one deadline around the whole turn, every
round and handler included; past it the turn `failed: limit_exceeded` and a running handler is
cancelled. Proved by `tests/test_backend_gateway.py`: `test_a_transport_that_never_answers_ends_at_the_limit`
and, in `tests/test_backend_gateway_tools.py`,
`test_a_deadline_during_a_handler_cancels_it_and_keeps_the_side_effect_state`.

What the client sends for a text turn, and nothing else:

| Part | Value |
| --- | --- |
| Request | `POST <origin>/v1/chat/completions`, no redirect followed, no cookie kept, no proxy variable read; HTTPS trusts `ca_file` alone |
| Headers | `Authorization: Bearer <key>`, `Content-Type: application/json`, and the transport's own defaults; no `x-litellm-*` or `anthropic-*` header |
| Body | exactly `model` (the request's `text/<model>` name), `messages` (the system prompt, then the turn's messages, then the round's calls and results), `user` and, when the agent has tools, `tools` (OpenAI function tools of each declaration's `name`, `description` and `parameters`); no `stream`, `max_tokens`, `metadata`, `tags`, `fallbacks`, `num_retries`, `mock_response` or `api_key` |
| `user` | `"fp-" + sha256(json.dumps([iss, sub], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()`, 67 characters |

Proved by `tests/test_backend_gateway.py`: `test_the_request_is_one_post_with_the_system_prompt_and_the_messages`,
`test_no_header_beyond_the_transports_own_defaults_is_sent`, `test_no_pinned_absent_field_is_sent`,
`test_a_redirect_is_not_followed_and_a_cookie_is_not_kept`, `test_the_proxy_environment_is_ignored`
and `test_https_is_trusted_through_the_configured_ca_file_alone`.

The fingerprint's prefix, input and algorithm are one contract across apps: a change splits
every person's budget, so every caller computes it with
`agent_runtime_client.turn.person_fingerprint`. Proved by
`test_the_public_fingerprint_and_the_gateway_user_give_the_documented_vectors`,
`test_a_different_principal_gets_a_different_fingerprint` and
`test_a_fingerprint_is_67_characters_and_the_same_for_one_principal`.

| The gateway answers | The turn ends |
| --- | --- |
| 200, `finish_reason: stop`, non-empty text of at most `MAX_EVENT_BYTES` | one `AssistantTextEvent`, then `FinalEvent` |
| 200, `finish_reason: tool_calls`, whole round | the calls run in order (see below), then the next request |
| 200 otherwise (malformed JSON, no choices, empty text, another `finish_reason`, a lone surrogate, a longer reply, a round that is not whole) | `failed: provider_error` |
| 429 | `refused: busy`; `retry_after_s` from the `Retry-After` seconds, at most `MAX_RETRY_AFTER_S`; an HTTP-date or no header gives none |
| another 4xx, a budget refusal included | `failed: provider_unavailable`, detail `gateway_refused`; the body is read within `MAX_EVENT_BYTES` only for the logged error type and code |
| 1xx, a 2xx other than 200, 3xx or 5xx (a `Retry-After` is ignored) | `failed: provider_unavailable`, detail `gateway_failed`; the body is read as for a 4xx |
| connection refused or reset, TLS failure, an origin the HTTP library refuses | `failed: provider_unavailable` |

Proved by `tests/test_backend_gateway.py`: `test_a_text_turn_ends_with_the_answer`,
`test_a_200_that_is_no_complete_text_is_provider_error`,
`test_busy_carries_the_standard_header_seconds_capped_or_none`,
`test_a_refusal_status_is_provider_unavailable_as_gateway_refused`,
`test_a_server_failure_is_provider_unavailable_and_ignores_retry_after`,
`test_a_lost_connection_is_provider_unavailable` and
`test_an_origin_the_http_library_refuses_is_provider_unavailable`. A person over budget reads as
"unavailable", the same as any other refusal. `detail` is for logs only. A refusal logs the
gateway's error type and code, never its message
(`tests/test_backend_gateway_refusal.py`).

### The tool loop

The loop runs in the client, in process, over buffered rounds. A round is judged whole before
anything shows or runs: only the request's tool names are offered and accepted, so a call to any
other name, arguments that are not a JSON object, an empty round or a call id used twice in the
turn refuses the round (`failed: provider_error`) with no handler run. A round that would take
the turn past `max_tool_calls` ends `failed: limit_exceeded` the same way. Proved by
`tests/test_backend_gateway_tools.py`: `test_a_round_that_is_not_whole_runs_nothing_and_shows_nothing`,
`test_a_call_to_a_tool_the_agent_did_not_select_is_refused`,
`test_a_call_id_reused_in_a_later_round_refuses_that_round_and_keeps_the_side_effect_state` and
`test_crossing_max_tool_calls_fails_with_limit_exceeded`.

For each call, in order: `ToolCallEvent`; the side-effect state set from the declaration's
`ToolEffect`, before the handler runs; `await handler(principal, arguments)`; `ToolResultEvent`.
Proved by `test_two_calls_in_one_round_run_in_order_each_between_its_events`. A call, or the next
request, that the deadline has already passed when its turn comes is not started:
`failed: limit_exceeded`, with the side-effect state unchanged by it
(`test_a_call_event_taken_past_the_deadline_starts_no_handler_and_leaves_no_record`,
`test_a_deadline_that_passed_while_the_app_took_the_result_event_sends_no_next_request`).

A `ToolOutcome(is_error=True)` goes to the model, which reads it as `Error: ` followed by the
content, and the turn goes on (`test_an_error_outcome_reaches_the_model_and_the_turn_goes_on`,
`test_the_model_reads_an_error_outcome_differently_from_a_success_of_the_same_text`). A handler
that raises is a host defect: the client logs its class name only and tells the model the call
failed (`test_a_raising_handler_logs_its_class_only_and_the_model_gets_an_error`). The one
exception is `TurnStopped`: it ends the turn unchanged, with no result event and no further round,
and `after_side_effect` is true once a side-effecting call started or the handler set it
(`test_a_handler_that_stops_the_turn_ends_it_with_that_exception_and_no_further_round`,
`test_a_handler_that_stops_the_turn_leaves_after_side_effect_as_the_connector_promises`). Task
cancellation reaches the caller unchanged and no later round runs
(`test_cancelling_between_a_call_and_its_result_reaches_the_caller_unchanged`). Per started call
one INFO record carries the tool name, call id and `is_error`, never the arguments or the
content (`test_each_call_logs_its_name_id_and_error_flag_never_arguments_or_content`). A call
that started may have written, and a cancelled `asyncio.to_thread` write may still finish;
nothing implies it was undone. A blocking tool stays the app's: its handler uses
`asyncio.to_thread`.

A tool's `content` reaches the model, and so the gateway and the provider the model names: a
tool returns only what that may see.

| Bound | Value | Over it |
| --- | --- | --- |
| One tool result | `MAX_EVENT_BYTES` | the model gets a fixed `is_error` sentence instead of the content; the side effect stands; the turn continues |
| One request of a turn, serialised | 4 × `MAX_EVENT_BYTES` | no request is sent; `failed: limit_exceeded` |
| One response body | as for a text turn | `failed: provider_error` |

Proved by `test_a_tool_result_over_the_event_bound_is_replaced_and_the_side_effect_stands` and
`test_repeated_large_results_stop_the_turn_before_a_request_over_the_bound`.

### A picture agent

A request with a `picture_size` runs after the same admission and under the same one deadline,
as one call: `POST <origin>/v1/images/generations` with a text turn's headers, client and
request bound. The body is exactly `model` (the request's `image/<model>` name), `prompt` (the
agent's prompt, a blank line, the message), `size` and `user` (the fingerprint above, so one
person has one budget in text and pictures); `n` and `response_format` are never sent. A picture
request holds exactly one user message and no tools, refused when the `TurnRequest` is built.
Proved by `tests/test_backend_gateway_pictures.py`: `test_the_call_is_one_post_to_the_images_path_with_the_headers_of_a_text_turn`,
`test_the_body_holds_exactly_model_prompt_size_and_user` and
`test_the_user_is_the_fingerprint_a_text_turn_of_the_same_person_sends`.

The reply holds exactly one `data` entry with `b64_json`. A non-null `url` is refused and never
fetched; `url: null` beside `b64_json` passes. The turn ends with
`FinalEvent(text="", files=(RunFile(name="picture", content=<the bytes>),))` and no
`AssistantTextEvent`. No media type travels: `RunFile` holds none, and the app sniffs the bytes.
Proved by `test_one_picture_ends_the_turn_as_a_file_with_no_other_event`,
`test_a_null_url_beside_the_picture_passes`, `test_a_non_null_url_is_never_fetched` and
`test_a_reply_that_is_not_exactly_one_canonical_picture_is_a_provider_error`.

| Bound or reply | Value | Over it or otherwise |
| --- | --- | --- |
| The HTTP body | `base64_length(MAX_RUN_FILES_BYTES)` plus `MAX_EVENT_BYTES` for the JSON around it | `failed: provider_error`, as a text body over its bound |
| The decoded picture | canonical base64, at most `MAX_RUN_FILES_BYTES` | `failed: provider_error` |
| 429, another status, a connection or read failure, the deadline | | as for a text turn |

Proved by `test_a_body_one_byte_over_the_read_bound_is_a_provider_error` and
`test_a_picture_one_byte_over_its_decoded_bound_is_a_provider_error`. A vendor's moderation
refusal is a 400 and so reads as `provider_unavailable`; an app shows that it did not create a
picture. Closing the stream cancels the HTTP call, and the vendor may still bill it. Which sizes
exist is the gateway's: the client checks the size's shape only.

### The model list

`await list_gateway_models(config.gateway, *, transport=None)` (in
`agent_runtime_client.backend.gateway`) answers a `GatewayModels` for an admin page, which has no
turn. It sends a bare `GET <origin>/v1/models` with a text turn's client rules and
`Authorization: Bearer <key>` alone. The key file is read per call. The whole call has one
deadline of 10 seconds and a reply body bound of `MAX_EVENT_BYTES`. Proved by
`tests/test_backend_gateway_models.py`: `test_the_request_is_a_bare_get_with_the_key_and_nothing_else`,
`test_the_key_is_read_again_for_each_call` and `test_a_body_at_its_bound_passes`.

`GatewayModels.text` and `.image` hold the `data[].id` names of each kind in the gateway's
order; an id that is no `text/…` or `image/…` model name is not offered and is no error.
`for_agent(agent)` answers the names of that `AgentDefinition`'s kind, the ones its
`request(messages, model=…)` accepts. Proved by `test_the_list_splits_text_and_image_names_in_the_gateways_order`,
`test_an_id_of_another_kind_is_not_offered_and_is_no_error` and
`test_for_agent_answers_the_names_of_that_agents_kind`.

Every failure raises the one `ModelListUnavailable`, whose `detail` (`key_unusable`,
`gateway_refused`, `gateway_failed`, `connection_failed`, `malformed_reply` or `timed_out`) is a
closed label; neither the key nor any text of the reply is in it, and the key is in neither the
exception's cause nor its traceback's frames. Proved by
`test_every_failure_raises_the_one_exception_without_the_key` and
`test_the_raising_frame_and_its_callers_hold_no_key_in_a_local`.

An app sets an agent's model with `agent.request(messages, model=name)`; without `model` the
file's model runs. A name of the wrong kind raises `ValueError` before any request
(`test_request_refuses_an_override_of_the_wrong_kind` in `tests/test_agents_file.py`). The
gateway may later withdraw a name an app stored: that turn ends `failed: provider_unavailable`
and the client does not fall back to the file's model.

`GatewayBackend`'s constructor and every `list_gateway_models` call set the `httpx2` and
`httpcore2` loggers to WARNING for the whole process, overriding an app's own DEBUG setting
there (`test_the_transport_loggers_that_could_carry_the_key_are_not_enabled_for_debug`,
`test_a_list_call_keeps_the_transport_loggers_off_debug_so_the_key_stays_out_of_them`).

## The person front

`agent_runtime_client.mcp_server.McpPersonFront(tools, *, resolve_person)` is the MCP endpoint of
an app for the agent of one signed-in person. It speaks the JSON-only profile of
`agent_runtime_client.mcp_wire` (POST only, answered in `application/json`, `MCP-Protocol-Version`
on every request after `initialize`, a notification answered with 202 and an empty body). It is
an ASGI 3 app the host registers as the exact route `MCP_ENDPOINT_PATH`, never a `Mount`, which
redirects `/mcp` to `/mcp/`, and behind the middleware that admits the caller: the host's gate
answers every 401 and 403 before routing, so the front answers no challenge. Proved by
`tests/test_mcp_server_person.py`:
`test_an_exact_starlette_route_serves_initialize_list_and_call_without_a_redirect`.

`resolve_person` is a port, `Callable[[Scope], Principal]`, and the front's one seam to the
host's authentication: the client imports no authentication library. The front calls it first on
every request, before it reads the body. Its failure propagates to the host: no tool runs and the
front writes no answer, because a front reached without the gate's admission is a host defect. A
result that is not a `Principal` raises `TypeError` the same way. Proved by
`test_a_resolver_failure_reaches_the_host_before_the_origin_check_and_the_body` and
`test_a_resolver_result_that_is_not_a_principal_reaches_the_host_before_the_body`.

A tool is `PersonTool(declaration, handler)`; the handler is
`PersonToolHandler = Callable[[PersonRequest, JsonObject], Awaitable[ToolOutcome]]`, and
`PersonRequest(principal, scope)` carries the resolved `Principal` and the request's ASGI scope,
which holds the bearer, `Host` and client address a handler replays. A name given twice raises
`ValueError` at construction. Proved by `test_a_call_hands_the_handler_the_resolved_person_and_the_request_scope`,
`test_concurrent_calls_each_get_the_person_and_the_scope_of_their_own_request` and
`test_two_tools_of_one_name_are_refused_at_construction`.

A request with an `Origin` header is 403 (its clients are agents, which send none), anything but
`POST` is 405 with `Allow: POST`, and a body over `MAX_EVENT_BYTES` is 413. Proved by
`test_a_request_carrying_an_origin_of_any_value_is_forbidden_and_runs_nothing`,
`test_anything_but_post_is_method_not_allowed_with_the_allowed_method_named` and
`test_a_body_one_byte_over_the_bound_is_too_large_and_runs_nothing`. `tools/list` offers every
tool, because rights are the route's, with `annotations.readOnlyHint` true for a `READ_ONLY` tool
and absent otherwise (`test_tools_list_shows_every_tool_with_the_read_only_hint_only_on_a_read_only_one`).

A handler that raises, or returns an outcome that cannot be written, answers a failed tool
result with one fixed text; the log names the tool and the exception's class, and nothing of its
message reaches the wire or the log
(`test_two_tools_failing_differently_answer_one_failed_result_without_their_messages`,
`test_an_outcome_that_cannot_be_written_answers_the_failed_result_and_leaks_nothing`). An answer
that carries the signature of the request's own bearer is replaced by an empty 500
(`test_a_result_carrying_the_request_bearer_is_withheld_and_the_log_names_no_caller_text`).

## The member module

`agent_runtime_client.member` owns what an app and an agent say to each other about a member:
the records above, `MemberStanding(standings)` (what `STANDING_URI`, `member://standing`,
answers with: a `WaitAnswer` without its cursor), `PlaceWindow(generation, starts_at, entries)`
of `PlaceEntry(seq, kind, name, text, own)`, `LISTEN_BOUND_SECONDS` (50) and
`AcceptInvitationArguments(place, invitation, epoch)` for the tool `ACCEPT_INVITATION_TOOL`.
A window's `seq` ascends strictly and may skip; a non-empty window starts at its first `seq`;
an empty window is valid and its `starts_at` is the `seq` the next line gets. A place's window
is the resource `place_window_uri(place)`, `member://standing/places/{kind}/{id}` with each
segment percent-encoded, so `/` in an opaque `kind` or `id` stays inside its segment;
`place_ref_from_window_uri(uri)` returns the `PlaceRef` for that canonical spelling only and
`None` for anything else. A URI is no credential. Proved by `tests/test_member.py`.

## The wait tool

Every app tells a member what concerns it the same way: through one bounded tool, `wait`, served
on its person front beside its other tools and read by the member's runner in a loop. The
answer and the protocol constants are in the base module `agent_runtime_client.wait`, and the
records it holds (`PlaceRef`, `StandingState`, `Invitation`, `Standing`, `LEASE_SECONDS`) are
owned by the base module `agent_runtime_client.member`, which `wait` re-exports; so a
runner parses an answer without the `mcp-server` extra; the tool and its ports are in
`agent_runtime_client.mcp_server`.

An answer is `WaitAnswer(cursor, standings)`: an opaque cursor and every place the member is in
now, never a list of changes. A `Standing` is a `PlaceRef(kind, id)`, opaque to all but the app,
a `StandingState` (`invited`, `active` or `paused`) and, exactly when invited, an
`Invitation(id, epoch, expires_at)`; any other combination is refused at construction. `epoch`
is the place's removal epoch, so the app refuses an accept after a removal. A paused place is
listed and a place the member left is absent, so a pause is told from a removal. Because each
answer is the whole standing, a late, repeated or lost answer leaves no stale state: the next
one is the truth again. Proved by `tests/test_member.py` and `tests/test_wait.py`:
`test_a_standing_carrying_an_invitation_other_than_exactly_when_invited_is_refused`,
`test_only_an_invited_standing_carries_its_invitations_id_epoch_and_expiry_on_the_wire`, and by
`tests/test_mcp_server_wait.py`: `test_a_paused_place_is_listed_and_a_place_left_is_absent`.

`wait_tool(changes, standings)` builds the read-only tool `wait(after?, timeout_seconds?)`. Its
recipient is the request's resolved `Principal`, `iss` and `sub` together; no argument names
one. `after` is a string of at most 128 characters and `timeout_seconds` an integer from 1 to
`WAIT_BOUND_SECONDS` (50), the bound when absent; anything else, or an extra argument, is the
tool's error naming the argument. Proved by
`test_the_listed_wait_tool_is_read_only_and_names_no_recipient`,
`test_one_subject_at_two_issuers_is_two_members_that_wake_and_read_apart` and
`test_a_refused_argument_is_the_tools_error_naming_it_with_no_standing`.

The cursor rules:

- Without `after`, with a string that is no cursor of this run, or with a cursor older than the
  member's latest change, the tool answers at once. A cursor of an earlier run, after a
  restart, is no cursor of this run. Proved by
  `test_a_first_wait_answers_at_once_with_every_standing_and_a_cursor`,
  `test_an_admitted_string_that_is_no_cursor_answers_at_once`,
  `test_a_retry_with_a_cursor_older_than_the_latest_change_answers_at_once` and
  `test_a_cursor_of_an_earlier_run_answers_at_once_after_a_restart`.
- With the current cursor it answers as soon as a change is signalled for the member, every
  wait of the member at once, or when `timeout_seconds` runs out with the unchanged standing; a
  retry with that cursor waits again. Proved by
  `test_a_signalled_change_ends_the_wait_before_its_timeout_with_the_new_standing`,
  `test_two_waits_of_one_member_both_answer_one_change` and
  `test_a_wait_with_the_current_cursor_answers_the_unchanged_standing_at_its_timeout_each_time`.
- Every answer carries the cursor taken before its standings were read. A cancelled wait sends
  nothing and consumes nothing. Proved by
  `test_a_cancelled_wait_sends_nothing_and_leaves_every_other_wait_of_the_member_wakeable`.

The read of the standings is bounded by `STANDINGS_READ_SECONDS` (5). A read that fails or runs
past it is the tool's failed result, never an empty standing, so a call ends within
`timeout_seconds` plus that bound, at most 55 seconds. Proved by
`test_a_failing_read_is_the_tools_error_and_the_front_answers_the_next_call`,
`test_a_read_past_its_deadline_is_the_tools_error_within_the_timeout_plus_the_deadline` and
`test_a_wait_without_a_timeout_ends_at_the_bound_plus_the_read`.

The app passes two ports in. `CurrentStandings`, `(recipient) -> standings`, is its own read of
its participation state, the one source of truth, and answers well inside the read bound.
`MemberChanges` is `changed(recipient)`, which never blocks and runs on the event loop that serves
the front, or from a thread through `anyio.from_thread.run_sync`, and `wait(recipient, after,
seconds) -> cursor`. `InMemoryMemberChanges` serves one process: a change counter per member
that ever had one, a run token in every cursor and no history, so it needs no capacity setting. Proved by
`test_the_in_memory_changes_grow_with_neither_changes_nor_finished_waits`.

The consistency rule every app keeps: it calls `changed(recipient)` only after the new state is
visible to `CurrentStandings`. The tool takes the cursor before it reads, so a change after a
read, even before its answer reaches the runner, lies after that answer's cursor, and the next
wait answers at once with a fresh read. A runner that keeps one wait in flight therefore loses
no change and never sees a standing older than the one before. A change signalled while a wait
starts is never lost. Proved by
`test_a_change_during_the_read_makes_the_next_wait_answer_at_once_with_the_new_standing` and
`test_a_change_signalled_at_any_step_of_a_starting_wait_is_never_lost`.

The lease: every call a member makes is an admitted call, and the app renews the member's lease
on each, `wait` included. A runner that calls again within `LEASE_SECONDS` (120) keeps its
lease, so a loop of waits, each ending within 55 seconds, renews itself.

What each app owns: its places and who stands where, the invitation, its expiry and the epoch
check at accept, the participation and permission decisions, calling `changed` after each
visible change, and enforcing the lease, removing a member whose lease ran out.

## A marked route as a tool

A host turns the routes of its FastAPI app into person-front tools. `tools.agent_tool(name=…,
effect=…)` is the mark, passed as a route's `openapi_extra`; an unmarked route is never a tool,
and the client imports no FastAPI. `mcp_server.route_tools(openapi, app, *, mark_route)` reads
the host's own `app.openapi()` and returns `RouteTools(tools, marks)`: the `PersonTool`s, and a
`frozenset` of what `mark_route(METHOD, template)` returned for each marked operation, in the
host's own mark type. `mark_route` runs only on marked operations; its `ValueError` becomes a
`RouteProfileError` naming the operation, with the host's error as its cause, and any other
exception propagates. Proved by `tests/test_mcp_server_routes.py`:
`test_a_marked_route_becomes_a_tool_with_its_name_description_and_input_schema`,
`test_an_unmarked_route_is_never_passed_to_the_host_mark_function`,
`test_the_host_mark_functions_value_error_becomes_a_profile_error_with_it_as_cause` and
`test_another_exception_from_the_host_mark_function_propagates_unchanged`.

The host wires in this order: register every route, read `app.openapi()` (FastAPI caches it),
run `route_tools`, then build the gate with the marks.

**The profile.** `GET` and `POST` only. Parameters are string path parameters and scalar query
parameters (string, integer, number or boolean, with or without `enum`, inline or by `$ref`,
nullable through `anyOf [x, null]`); the one body is a required `application/json` object whose
fields are flattened beside the parameters; `$ref` is inlined from `components`, and an
annotation beside a reference is kept and wins over the component's own; every success response
is JSON, and an error status may also declare RFC 9457 problem details
(`application/problem+json`), beside or instead of JSON. The tool's description is the
operation's `description` (the route's docstring) and is required. Everything else is refused at
start with `RouteProfileError`: a mark on any other method, a mark that is not the mapping
`agent_tool` returns, header and cookie parameters, a non-JSON or optional body, a success
response that is not JSON, an error response that is neither JSON nor problem details, a
recursive schema, a body object carrying any keyword flattening would lose, a `$ref` beside any
keyword that is not an annotation, a name shared by two inputs, two marks of one name, an integer
path parameter and a missing description. Proved by
`test_a_marked_route_outside_the_profile_is_refused_naming_the_operation`,
`test_an_error_status_may_declare_problem_details`,
`test_a_hand_written_mark_that_agent_tool_does_not_return_is_refused`,
`test_a_body_object_keyword_that_flattening_would_lose_is_refused`,
`test_a_constraint_beside_a_reference_is_refused` and `test_two_marks_of_one_name_are_refused`.

**Overlap.** Two templates of one method overlap when they have the same number of segments and
every pair of segments at a position could match one string: two literals only when equal, and
any pair holding a parameter always. A marked template that overlaps any other operation of its
method is refused whatever the order; a partial segment may refuse a pair that never collides,
never the reverse. Unmarked templates are read, never passed to `mark_route`. The document
cannot show converters, routes hidden from it, a `Mount`, a catch-all or a second registration of
one method and path; proving those is the host's duty. Proved by
`test_a_marked_template_that_overlaps_another_operation_of_its_method_is_refused_in_either_order`
and `test_a_marked_template_that_cannot_collide_with_another_operation_is_accepted`.

**The call.** Each tool call is one in-process ASGI request into `app`, which must be the host's
whole application, so the request passes every middleware. An argument the schema does not name,
a path value that is missing, not a string, empty, `.`, `..` or holding `/`, and an object or
array query value fail the call before any inner request
(`tests/test_mcp_server_route_call.py`:
`test_a_bad_path_value_or_an_unnamed_argument_is_refused_with_no_inner_request`,
`test_an_object_or_array_query_value_is_refused_with_no_inner_request`). `path` is the outer
`root_path` followed by the template with each value as given, and `raw_path` has each value
percent-encoded as UTF-8; nothing decodes either again
(`test_path_values_keep_the_decoded_path_apart_from_the_encoded_raw_path`). An absent or `null`
query value is omitted, the empty string is sent as `name=`, a string as itself and any other
scalar as its JSON text (`test_query_values_reach_the_route_as_sent`). The inner scope holds only
`type`, `asgi`, `http_version`, `scheme`, `server`, `client` and `root_path` from the outer one,
plus the call's own method, path, `raw_path`, query and headers: `Authorization`, `Host`, every
`X-Forwarded-For` line, `X-Forwarded-Proto`, and the generated `Content-Length` (and
`Content-Type: application/json` with a body). It carries no cookie, `Origin`,
`Accept-Encoding` or `state`, so the host's gate admits the inner request on its own, and a
route reading lifespan state from `scope["state"]` fails visibly
(`test_the_inner_scope_carries_exactly_the_allowlisted_keys`,
`test_the_inner_request_replays_exactly_the_named_headers`). `receive` gives the body once, then
waits until the response is complete and answers `http.disconnect`
(`test_receive_gives_the_body_once_then_blocks_until_the_response_is_complete`).

**Outcomes.** A 2xx gives the body text and a 4xx a failed result with the route's body. A 3xx, a
5xx, a body over `MAX_EVENT_BYTES` (no byte past it is kept), a body that is not UTF-8 and an app
that returns before its response is complete give a failed result with one fixed text; only the
status is logged. An exception the app raises propagates to the front, which answers its own
fixed text and logs the class. Proved by `test_a_2xx_gives_the_body_and_a_4xx_gives_a_failed_result_with_the_routes_body`,
`test_an_unusable_response_gives_one_failed_result_that_repeats_nothing_of_the_body`,
`test_a_body_exactly_at_the_bound_is_given` and `test_an_exception_the_app_raises_propagates`.
