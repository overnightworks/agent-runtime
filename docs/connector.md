# Connector reference

For the developer of an app that runs agent turns through `agents`: the turn an
app starts, the agent files it ships, the tools it declares, the gateway backend, the MCP
person front and the voice loop. The standard path is that the app serves its member's standing as MCP resources and
announces a change through the standard's subscriptions; [the standard path](#the-standard-path)
says what a host wires and what a runner relies on. Each rule names the test that proves it. The
docstrings of `agent_file`, `tools`, `turn`, `backend` and `mcp_server` are exact where this file is
short; the shape of the client is in [ARCHITECTURE.md](ARCHITECTURE.md).

## The turn

An app runs a turn through `agents.backend.AgentBackend`, installed as
`src/<app>/agent/backend.py` with its tools in `src/<app>/agent/tools.py`.
`backend.run(request, person)` is an async generator of `TurnEvent`s that the app consumes inside
`contextlib.aclosing`; closing it runs the backend's cleanup. The base modules import no HTTP
library; the one implementation, `backend.gateway.GatewayBackend`, needs the `backend` extra.

`request` is a `TurnRequest` (`agents.agent_file`, frozen): `agent`, `turn`, `roles`,
`tools` (the agent's selected `ToolDeclaration`s), `model`, `max_duration_seconds`,
`max_tool_calls` (`None` exactly when `tools` is empty) and `picture_size` (`None` for a text
agent). `AgentDefinition.request(messages, *, model=None, appended_system=None)` builds it;
`model` overrides the file's model for that run, with a model of the agent's kind, and
`appended_system` follows the file's prompt in the system text, after a blank line.
Proved by `tests/test_agent_file.py`: `test_request_carries_the_files_prompt_model_roles_and_tools`,
`test_request_model_is_the_override_when_given`, `test_request_is_frozen`; the appended text by
`tests/test_voice_loop.py`: `test_a_spoken_line_that_wakes_sends_one_request_with_prompt_protocol_context_and_the_line`.

`person` is a `TurnPerson` (frozen, always a person): `principal` (the verified `(iss, sub)`),
`role` (the app role) and `groups`, an async callable returning the person's groups. Proved by
`tests/test_backend_port.py`: `test_a_turn_person_is_not_constructible_without_a_principal`.

A turn ends short of its answer with one exception, `TurnStopped(kind, code, retry_after_s,
after_side_effect, detail)`: `refused` carries a `RejectionCode`, `failed` a `FailureCode`. A cancel
reaches the caller as `CancelledError`. `after_side_effect` is true once a side-effecting tool call
started in the turn, whatever ended it, `refused: busy` included, so an app does not retry a turn
that may have written. Proved by `tests/test_backend_gateway_tools.py`:
`test_a_busy_gateway_at_round_two_reports_whether_a_call_may_have_written`.

`TurnStopped.outcome` is what the stop means to a person, a `StopOutcome` derived from the code and
never stored beside it:

| Code | Outcome |
| --- | --- |
| `refused: not_enabled`, `refused: forbidden` | `not_enabled` |
| `refused: busy` | `busy` |
| `refused: budget_spent` | `budget_spent` |
| every `failed` code | `unavailable` |

`retry_after_s` comes only with `busy`, as the gateway gave it; the outcome never changes it. Proved
by `tests/test_backend_port.py`: `test_every_stop_code_has_exactly_its_outcome`,
`test_the_outcome_table_covers_every_stop_code`,
`test_a_turn_stop_and_a_failed_decision_of_one_code_give_the_same_outcome`,
`test_a_busy_refusal_is_busy_with_or_without_a_wait` and
`test_an_outcome_is_never_given_apart_from_its_code`.

`AssistantTextEvent` is a piece of the assistant's text, yielded as the gateway streams it, before
its round is judged. Pieces come from every round, including a round that ends in tool calls: its
pieces come before its `ToolCallEvent`s. `FinalEvent.text` is the last round's judged text only, so
when tools ran, the turn's pieces joined need not be `FinalEvent.text`; the last round's pieces
joined are. A turn that ends with `TurnStopped` may have yielded pieces first; nothing withdraws
them. An app that shows only judged text reads `FinalEvent`. Proved by
`tests/test_backend_gateway_stream.py`:
`test_each_text_piece_comes_as_its_own_event_in_order_then_the_joined_answer`,
`test_a_piece_reaches_the_app_while_its_round_is_still_open`,
`test_a_tool_rounds_pieces_come_before_its_calls_which_run_after_it_and_echo_whole` and
`test_a_round_that_streams_text_then_fails_leaves_the_text_shown_and_runs_nothing`.

`FinalEvent.usage` is a `TurnUsage` (`agents.turn`): four non-negative counts summed over every
round of the turn. `input_tokens` is everything the model read, cached tokens included;
`cached_input_tokens` is the part read from the provider's cache, `cache_write_tokens` the part
written to it, `output_tokens` what the model wrote. A round's usage is the gateway's last usage
chunk: `prompt_tokens`, `prompt_tokens_details.cached_tokens` (absent: 0),
`prompt_tokens_details.cache_write_tokens` else `cache_creation_input_tokens` else 0, and
`completion_tokens`. The usage is read from a chunk with `choices: []`, with `choices` absent or
null, or alongside a choice; a chunk with neither `choices` nor a `usage` key fails the round as a
malformed reply. A chunk whose `usage` key holds null carries no usage: the round's usage is the
last non-null one, and a round with none has no usage. `usage` is `None`, never a partial sum, when
any round sent none or one with a missing, negative or non-integer count, and for a picture turn;
reading usage changes nothing else the turn yields. The library passes the gateway's counts through
unchecked: it does not verify that `cached_input_tokens` is no larger than `input_tokens`. A turn
that ends with `TurnStopped` reports none, though its rounds were paid. Proved by
`tests/test_backend_streamed_round.py`:
`test_a_usage_chunk_with_no_choice_gives_the_rounds_usage`,
`test_usage_on_a_chunk_that_carries_a_choice_gives_the_rounds_usage`,
`test_a_later_chunk_with_null_usage_keeps_the_usage_already_read` and
`test_unreadable_usage_gives_a_round_with_no_usage_and_the_same_message`; and by
`tests/test_backend_gateway_usage.py`: `test_the_final_event_carries_the_usage_the_gateway_sent`,
`test_a_usage_chunk_without_choices_gives_the_turn_its_usage`,
`test_a_usage_chunk_with_a_null_usage_ends_the_turn_with_no_usage`,
`test_a_chunk_with_neither_choices_nor_usage_still_stops_the_turn`,
`test_the_final_events_usage_sums_the_usage_of_every_round`,
`test_a_turn_whose_round_has_no_readable_usage_ends_with_no_usage_and_the_same_text`,
`test_a_turn_with_a_round_that_has_no_usage_ends_with_no_usage` and
`test_a_turn_that_stops_after_a_paid_round_yields_what_it_yields_without_usage`.

The app's configuration embeds `agents.config.AgentConfig` as the `agent` section:
`backend` (`litellm`, the one value), `gateway` and `api_group`, none with a default.
`ChatGateway` is `origin` (scheme, host and optional port; no path, query, fragment or
userinfo), `key_file` and `ca_file`, both absolute paths. `ca_file` is required for HTTPS and
forbidden for HTTP. A missing or malformed value fails loud. Proved by
`tests/test_backend_config.py`: `test_a_missing_host_fact_fails_loud`,
`test_a_malformed_origin_fails_loud`, `test_a_malformed_file_setting_fails_loud`.

### What every app does the same

Every app names the same files: `agent/backend.py` builds the backend, `agent/tools.py` holds
its tools, `agent/mcp.py` its MCP surface, and `agent/settings.py` its `agent` configuration
section. `agent/settings.py` is the one place that imports `agents.config`.

`TurnPerson` is always the verified person, and `principal.iss` is passed exactly as the app
bound it: two spellings of one issuer split one person's budget. `busy` may carry no
`retry_after_s`, so the app needs a defined fallback; a value that varies by app stays the
app's. An app maps only `TurnStopped.outcome` to its wording and keeps `code` and `detail` for its
logs; a person over budget learns that the budget is spent, never an outage.

Tests use a fake `AgentBackend` that yields `TurnEvent`s and raises `TurnStopped`. A proof
through the real entry point runs `GatewayBackend` on `backend.fake_gateway.FakeGateway`, a
scripted transport passed as `transport=`.

## Agent files

An app declares its agents as Claude Code subagent files, `src/<pkg>/agents/<name>.md`, that
`agents.agent_file.load_agent` reads into an `AgentDefinition`: YAML front matter
(`name`, `description`, `tools`, `model`, `maxDurationSeconds`, `maxToolCalls`, `picture`,
`voice`, plus the extension `appRoles`, exactly `[admin]` or `[admin, user]`) and the Markdown
body as the prompt.

- `model` is a `text/<model>` name for a text agent (lower-case letters, digits, `.` and `-`)
  and `maxDurationSeconds` a positive integer; both are required. Proved by
  `tests/test_agent_file.py`: `test_a_file_without_a_required_key_is_refused_and_names_it`,
  `test_a_model_that_is_not_a_text_gateway_name_is_refused_and_named`.
- `maxToolCalls` is a positive integer, required exactly when `tools` is not empty and refused
  when it is. Proved by `test_a_missing_tool_call_bound_is_refused_when_there_are_tools` and
  `test_a_tool_call_bound_is_refused_when_there_are_no_tools` in the same file.
- `picture: {size: <width>x<height>}` (positive whole numbers, nothing else in the mapping)
  makes a picture agent: its `model` must be an `image/<model>` name, and it selects no `tools`
  and carries no `maxToolCalls`. Proved by
  `test_a_picture_file_that_does_not_fit_a_picture_agent_is_refused_and_named`.
- `voice: {windowBytes: <positive integer>}` (nothing else in the mapping) lets the agent run
  the voice loop and bounds what each of its decisions sends (see *The voice loop*);
  `AgentDefinition.voice_window_bytes` carries it, and a picture agent carries none, neither in
  its file nor in an `AgentDefinition` built by hand. Proved by
  `test_a_voice_agent_carries_its_window_and_any_other_agent_none`,
  `test_a_voice_section_that_is_not_one_positive_window_is_refused_and_named`, the picture
  test above and `test_a_definition_that_breaks_a_file_rule_is_refused_where_it_is_built`.
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

`agents.tools.ToolDeclaration` is the one declaration of a tool: name,
description, arguments and a `ToolEffect`, `read_only` or `side_effecting`. The effect is for the
host (`GatewayBackend` reads it for `after_side_effect`) and never reaches a prompt; it defaults
to side-effecting, because a tool nobody classified must be assumed to change state. Proved by
`test_the_request_carries_only_model_messages_user_stream_options_and_tools_without_the_effect` in
`tests/test_backend_gateway_tools.py`.

`HandTool` is a declaration with the async handler `(Principal, JsonObject) -> ToolOutcome` that
serves it in the app's own process; `ToolOutcome` is `content` and `is_error`. The module
exports `Principal` and `JsonObject` too, so an app's `agent/tools.py` imports only
`agents.tools`. `JsonObject` is a `dict[str, JsonValue]` (pydantic's `JsonValue`), so a handler
narrows a nested value with `isinstance` before it reads it. The module imports neither `mcp_wire`
nor anything of the MCP extra (`.importlinter` proves it).

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
(`test_debug_logging_writes_the_key_into_no_record`). However the turn stops, the key and the
reply's text are in none of the frames the gateway path leaves in the `TurnStopped`'s traceback. A
stop the gateway path raises leaves `run` as a fresh exception, raised from a frame holding
neither, with no underlying error in its `__context__` or `__cause__`
(`test_a_stopped_turn_holds_neither_the_key_nor_the_reply_in_any_frame_or_chained_error`). A stop
the host raises, from a handler or the groups lookup, stays the same exception, with the gateway
path's frames cut from its traceback
(`test_a_handlers_stop_after_a_tool_round_comes_back_the_same_and_holds_neither_key_nor_reply`).

**The deadline.** `request.max_duration_seconds` is one deadline around the whole turn, every
round and handler included; past it the turn `failed: limit_exceeded` and a running handler is
cancelled. A stream that stalls ends at the deadline, and an app that pulls the next event after it
gets `failed: limit_exceeded` with no further read. Proved by `tests/test_backend_gateway.py`:
`test_a_transport_that_never_answers_ends_at_the_limit`; in `tests/test_backend_gateway_tools.py`,
`test_a_deadline_during_a_handler_cancels_it_and_keeps_the_side_effect_state`; and in
`tests/test_backend_gateway_stream.py`,
`test_a_gateway_that_stalls_after_a_piece_ends_the_turn_at_the_deadline` and
`test_an_app_that_pulls_after_the_deadline_gets_limit_exceeded_and_nothing_more_is_read`.

What the client sends for a text turn, and nothing else:

| Part | Value |
| --- | --- |
| Request | `POST <origin>/v1/chat/completions`, no redirect followed, no cookie kept, no proxy variable read; HTTPS trusts `ca_file` alone |
| Headers | `Authorization: Bearer <key>`, `Content-Type: application/json`, and the transport's own defaults; no `x-litellm-*` or `anthropic-*` header |
| Body | exactly `model` (the request's `text/<model>` name), `messages` (the system prompt, then the turn's messages, then the round's calls and results), `user`, `stream` (`true`), `stream_options` (`{"include_usage": true}`, on every round) and, when the agent has tools, `tools` (OpenAI function tools of each declaration's `name`, `description` and `parameters`); no `max_tokens`, `metadata`, `tags`, `fallbacks`, `num_retries`, `mock_response`, `api_key` or cache marker |
| `user` | `"fp-" + sha256(json.dumps([iss, sub], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()`, 67 characters |

Proved by `tests/test_backend_gateway.py`: `test_the_request_is_one_post_with_the_system_prompt_and_the_messages`,
`test_no_header_beyond_the_transports_own_defaults_is_sent`, `test_no_pinned_absent_field_is_sent`,
`test_a_redirect_is_not_followed_and_a_cookie_is_not_kept`, `test_the_proxy_environment_is_ignored`
and `test_https_is_trusted_through_the_configured_ca_file_alone`.

The same values in the same order give the same bytes, and within a turn a round's body begins
with the round before: its `model`, its `tools` and its messages up to that round's last message
are byte-identical. The library sorts nothing, so a host that wants the provider's cache to hit
across turns keeps its tool order and its schemas' key order. Proved by
`tests/test_backend_gateway_usage.py`:
`test_a_second_rounds_body_begins_with_the_first_rounds_model_tools_and_messages`.

The fingerprint's prefix, input and algorithm are one contract across apps: a change splits
every person's budget, so every caller computes it with
`agents.turn.person_fingerprint`. Proved by
`test_the_public_fingerprint_and_the_gateway_user_give_the_documented_vectors`,
`test_a_different_principal_gets_a_different_fingerprint` and
`test_a_fingerprint_is_67_characters_and_the_same_for_one_principal`.

| The gateway answers | The turn ends |
| --- | --- |
| 200, an event stream that ends with `[DONE]` after `finish_reason: stop`, non-empty text of at most `MAX_EVENT_BYTES` | one `AssistantTextEvent` per text piece as it arrives, then `FinalEvent` |
| 200, an event stream that ends with `[DONE]` after `finish_reason: tool_calls`, whole round | the calls run in order (see below), then the next request |
| 200 otherwise (a stream that ends or breaks before `[DONE]`, a `[DONE]` with no finish reason, an event that is no JSON object or no chunk, a line that is no UTF-8, empty text, another `finish_reason`, a lone surrogate, a longer reply, a round that is not whole) | `failed: provider_error`, detail `malformed_reply`; pieces already yielded stand |
| 200, an event stream with an error event (a JSON object with an `error` key), with or without a following `[DONE]` | `failed: provider_unavailable`, detail `gateway_failed`; the log carries the error's type and code when they are well formed, never its message |
| 429 | `refused: busy`; `retry_after_s` from the `Retry-After` seconds, at most `MAX_RETRY_AFTER_S`; an HTTP-date or no header gives none |
| 422 whose error `type` is `budget_exceeded` (a spent person or app budget) | `refused: budget_spent`; decided from the status and the checked error type, never the message |
| another 4xx | `failed: provider_unavailable`, detail `gateway_refused`; the body is read within `MAX_EVENT_BYTES` only for the logged error type and code |
| 1xx, a 2xx other than 200, 3xx or 5xx (a `Retry-After` is ignored) | `failed: provider_unavailable`, detail `gateway_failed`; the body is read as for a 4xx |
| connection refused or reset, or lost while the stream runs, TLS failure, an origin the HTTP library refuses | `failed: provider_unavailable`, detail `connection_failed` |

Proved by `tests/test_backend_gateway.py`: `test_a_text_turn_ends_with_the_answer`,
`test_a_200_that_is_no_complete_text_is_provider_error`,
`test_busy_carries_the_standard_header_seconds_capped_or_none`,
`test_a_refusal_status_is_provider_unavailable_as_gateway_refused`,
`test_a_server_failure_is_provider_unavailable_and_ignores_retry_after`,
`test_a_lost_connection_is_provider_unavailable` and
`test_an_origin_the_http_library_refuses_is_provider_unavailable`; and by `tests/test_backend_gateway_stream.py`:
`test_a_round_that_streams_text_then_fails_leaves_the_text_shown_and_runs_nothing`,
`test_an_error_event_ends_the_turn_and_logs_only_its_type_and_code`,
`test_a_connection_lost_while_the_stream_runs_is_connection_failed` and
`test_a_stopped_turn_holds_no_text_from_the_wire`. A round is complete only at `[DONE]` after
`stop` or `tool_calls`; the client judges what the gateway sends, so a provider stream the
gateway cuts and closes with a finish reason of its own reads as complete. A person or app over budget reads as
`budget_spent`, no outage. `detail` is for logs only. A refusal logs the
gateway's error type and code, never its message
(`tests/test_backend_gateway_refusal.py`, which also proves `budget_spent`).

### The tool loop

The loop runs in the client, in process, over streamed rounds, each read whole before it is
judged. A round is judged whole before anything runs; its text shows as it streams. Only the
request's tool names are offered and accepted, so a call to any other name, arguments that are
not a JSON object, an empty round or a call id used twice in the turn refuses the round
(`failed: provider_error`) with no handler run. A round that would take the turn past
`max_tool_calls` ends `failed: limit_exceeded` the same way. Proved by
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
| One line, one event's data | 6 × `MAX_EVENT_BYTES` + 64 KiB | `failed: provider_error` |
| One round's text | `MAX_EVENT_BYTES`, judged per piece | `failed: provider_error`; the crossing piece is not shown |
| One assembled round | the request bound | `failed: provider_error`; no handler runs |

Proved by `test_a_tool_result_over_the_event_bound_is_replaced_and_the_side_effect_stands`,
`test_repeated_large_results_stop_the_turn_before_a_request_over_the_bound`,
`test_a_line_that_never_ends_is_cut_at_the_bound_as_provider_error` and
`test_a_round_past_the_request_bound_runs_no_handler`; the crossing piece by
`test_a_round_that_streams_text_then_fails_leaves_the_text_shown_and_runs_nothing`; the reader's
own bounds by `tests/test_backend_streamed_round.py`.

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
`agents.backend.gateway`) answers a `GatewayModels` for an admin page, which has no
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
(`test_request_refuses_an_override_of_the_wrong_kind` in `tests/test_agent_file.py`). The
gateway may later withdraw a name an app stored: that turn ends `failed: provider_unavailable`
and the client does not fall back to the file's model.

`GatewayBackend`'s constructor and every `list_gateway_models` call set the `httpx2` and
`httpcore2` loggers to WARNING only where the app left their level unset; a level the app set,
quieter or louder, stands
(`test_a_transport_logger_the_host_left_unset_is_not_enabled_for_debug_after_a_construction`,
`test_a_level_the_host_set_on_a_transport_logger_stands_after_a_construction`,
`test_a_transport_logger_the_host_left_unset_is_not_enabled_for_debug_after_a_list_call`,
`test_a_level_the_host_set_on_a_transport_logger_stands_after_a_list_call`). The key stays out of
the log even under DEBUG, because `httpcore2` prints a request by its method only
(`test_debug_logging_writes_the_key_into_no_record`,
`test_a_list_call_writes_the_key_into_no_record_under_debug_on_the_transport_loggers`).

## The person front

`agents.mcp_server.McpPersonFront(tools, *, resolve_person)` is the MCP endpoint of
an app for the agent of one signed-in person. It serves MCP revision 2026-07-28 on the official
SDK and nothing older: there is no `initialize` handshake and no legacy path. `server/discover`,
`tools/list` and `tools/call` are POST only and answered in `application/json`. It is an ASGI 3 app
the host registers as the exact route `MCP_ENDPOINT_PATH` (in `agents.mcp_wire`),
never a `Mount`, which redirects `/mcp` to `/mcp/`, and behind the middleware that admits the
caller: the host's gate answers every 401 and 403 before routing, so the front answers no challenge.
The host enters `async with front.running():` once in its own lifespan, the one thing the SDK's
session manager needs; a request outside it raises `RuntimeError`. The front runs on `asyncio`:
the allowance timer of a listen uses the running loop, which `anyio` alone does not offer. Proved by
`tests/test_mcp_server_person.py`:
`test_an_exact_starlette_route_serves_discovery_list_and_call_without_a_redirect`,
`test_a_request_outside_running_fails_loud`.

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

Resources and subscriptions, the standard's way for an agent to learn what changed, are one
more port and one more object on the front: see [the standard path](#the-standard-path).

A thin ASGI shell stands before the SDK and keeps this order. A request with an `Origin` header is
403 (its clients are agents, which send none), anything but `POST` is 405 with `Allow: POST`, a
body over `MAX_EVENT_BYTES` is 413, and a body with a repeated member, `NaN`, `Infinity` or a
number that overflows is refused before dispatch with the JSON-RPC parse error. Every request that
is not explicitly revision 2026-07-28, an absent or older `MCP-Protocol-Version` and `initialize`
included, gets `-32022` naming the served version. Proved by
`test_a_request_carrying_an_origin_of_any_value_is_forbidden_and_runs_nothing`,
`test_anything_but_post_is_method_not_allowed_with_the_allowed_method_named`,
`test_a_body_one_byte_over_the_bound_is_too_large_and_runs_nothing`,
`test_a_body_with_a_repeated_member_or_a_non_json_number_is_refused_before_dispatch` and
`test_anything_but_a_request_at_the_served_revision_is_refused_naming_it`. `tools/list` offers every
tool, because rights are the route's, with `annotations.readOnlyHint` true for a `READ_ONLY` tool
and absent otherwise (`test_tools_list_shows_every_tool_with_the_read_only_hint_only_on_a_read_only_one`).
The SDK answers the rest of the protocol (`Mcp-Method` mismatch `-32020`, missing `_meta` `-32602`,
unknown method 404): `test_the_sdk_ladder_answers_a_malformed_request_with_its_standard_error`.

A handler that raises, or returns an outcome that cannot be written, answers a failed tool
result with one fixed text; the log names the tool and the exception's class, and nothing of its
message reaches the wire or the log
(`test_two_tools_failing_differently_answer_one_failed_result_without_their_messages`,
`test_an_outcome_that_cannot_be_written_answers_the_failed_result_and_leaks_nothing`). An answer
that carries the signature of the request's own bearer is replaced by an empty 500
(`test_a_result_carrying_the_request_bearer_is_withheld_and_the_log_names_no_caller_text`).

From the first `running()` on, whichever of the SDK's server loggers (`mcp.server`, `mcp.shared`)
the host left unset is held above every level, and stays held after `running()` ends, so a
credential the SDK would log (a request's method, a handler failure) reaches no log even when the
host sets its root logger to DEBUG. A level the host sets on one of those loggers itself wins
(`test_running_holds_only_an_sdk_logger_the_host_left_unset`). The front's own logger names only
the tool and the exception's class
(`test_no_credential_marker_reaches_any_log_under_debug_or_normal_logging`).

## The member module

`agents.member` owns what an app and an agent say to each other about a member:
`PlaceRef`, `StandingState`, `Invitation` and `Standing`, `MemberStanding(standings)` (what
`STANDING_URI`, `member://standing`, answers with), `PlaceWindow(generation, starts_at, entries)`
of `PlaceEntry(seq, kind, name, text, own, spoken)`, `LEASE_SECONDS` (120),
`INVITATION_SECONDS` (120), `LISTEN_BOUND_SECONDS` (50),
`LISTEN_ALLOWANCE_SECONDS` (5), `AcceptInvitationArguments(place, invitation, epoch, figure, voice)` for the tool
`ACCEPT_INVITATION_TOOL`, and the records of the tool `SPEAK_TOOL`
([hearing and speaking](#hearing-and-speaking)). `spoken` is true for a line said aloud and
heard, false for a written one; every dumped entry carries it, and an entry without it reads as
written. A window's `seq` ascends strictly and may skip; a non-empty window starts at its first
`seq`; an empty window is valid and its `starts_at` is the `seq` the next line gets. A place's
window is the resource `place_window_uri(place)`, `member://standing/places/{kind}/{id}` with each
segment percent-encoded, so `/` in an opaque `kind` or `id` stays inside its segment;
`place_ref_from_window_uri(uri)` returns the `PlaceRef` for that canonical spelling only and
`None` for anything else. A URI is no credential.

**A place's view.** A place whose state is no conversation serves the member a
`PlaceView(revision, awaits_you, state)` instead of a window: `state` is the app's JSON object,
carried untouched and never read by the library (a value that is no JSON value is refused);
`revision`, an integer from 0, changes whenever the place moves on for this member, that is
whenever what the member may do or must act on changes, while parts of `state` that say only who
is present may change without it; `awaits_you` says the place waits for this member to act, and
the app decides it from the state at that revision, so one revision has one `awaits_you`. The
view is the resource `place_view_uri(place)`, the place's window URI with the segment `view`
after it; `place_ref_from_view_uri(uri)` returns the `PlaceRef` for that canonical spelling only
and `None` for anything else. A `Standing` carries `serves_view`, false unless set and then left
out of its JSON, so a standing that does not set it dumps without the key. The listing rule: an
app that sets `serves_view` on a standing lists that place's view URI among the member's current
resources; otherwise no pulse for the view is delivered. A place that serves both a window and a
view is not specified. Proved by `tests/test_member.py`, `tests/test_mcp_server_resources.py` and
`tests/test_mcp_server_listen_view.py`.

An accept may name the agent's 3D figure, `figure` as `FigureRef(url, sha256)`, and the `voice` it
speaks with; both are optional, and an accept without them dumps without their keys. The record
checks only their form: `url` is an absolute `https` address with a host and no user part, query or
fragment, `sha256` is 64 lowercase hexadecimal characters, and `voice` is 1 to 64 ASCII letters
of either case, digits, `.`, `_` or `-`. The app decides the rest:

- It fetches a figure only from sources it allows, and serves only bytes whose sha256 matches.
- It passes the voice to its speech service unchanged; the library folds no case. A voice that
  service does not offer is refused there, and the app answers with the refusal it already has; the
  library adds no refusal member for it.
- An absent field means the app's own default.

A `Standing` is a `PlaceRef(kind, id)`, opaque to all but the app, a `StandingState` (`invited`,
`active` or `paused`) and, exactly when invited, an `Invitation(id, epoch, expires_at)`; any
other combination is refused at construction. `epoch` is the place's removal epoch, so the app
refuses an accept after a removal. A paused place is listed and a place the member left is
absent, so a pause is told from a removal.

An `accept_invitation` refusal is an `is_error` result whose content is
`AcceptInvitationRefused(refused)` as JSON, in `speak`'s shape (`SpeakRefused`), with one
`AcceptInvitationRefusal` code: `stale_invitation` (the invitation is no longer standing, has
expired, or its id or epoch no longer matches, or the member holds no lease),
`two_seats` (the person who brought the member does not hold exactly one seat in the place, the
inviting one), `sponsor_away` (that person's seat lost its connection and waits to resume) or `busy`
(the place or the app already holds as many members as it takes, or that person already brings
another). A code the app adds for every tool alike is its own and not part of the record; a runner
reading it gets a validation error and decides itself. Proved by `tests/test_member.py`.

How a person calls an agent to a place and sends it away is the same in every app:
[calling.md](calling.md).

## The standard path

An agent learns what changes through the standard's resources and subscriptions, never by
polling a tool: the app serves `member://standing` and one window or view per place, and tells the
agent when one changed. This section is what a host wires and what a runner may rely on.

**The host** passes `resources=` and `changes=` to `McpPersonFront` and enters
`async with front.running():` in its lifespan (see above). `resources` is one port,
`MemberResources` (exported by `agents.mcp_server`): `current(principal)` returns
every URI that principal may read now, and `read(principal, uri)` returns the app's current
`MemberStanding`, `PlaceWindow` or `PlaceView` for it, or `None` when it is not that principal's. The principal
is always the request's verified one and no argument names a recipient. `changes` is one
`ResourceChanges` (exported by `agents.mcp_server`), without the port a `ValueError`:
the host calls `changes.changed(recipient, uri)` for the recipient's `Principal` only after the
change is visible to a read, on the front's event loop or through `anyio.from_thread.run_sync`; a
call for a principal with no open stream does nothing. `server/discover` advertises `resources`
with the port and `resources.subscribe` true with `changes`, and no `listChanged`.

**The answers.** `resources/list` answers exactly `current`, sorted, each entry with the URI as its
`name` and `application/json`. `resources/read` answers one text content with the requested URI,
`application/json` and the record's JSON. A `None` answers `-32602` with one fixed text that does
not echo the URI, so an unknown, a foreign and a non-canonical URI look alike. A port that raises
answers `-32603` with one fixed text and logs only the exception's type. Both carry `ttlMs: 0` and
`cacheScope: "private"`. `resources/templates/list` is not served. Proved by
`tests/test_mcp_server_resources.py`.

**The runner** listens with `resourceSubscriptions`, waits for the acknowledgement, which is the
first frame and holds the requested URIs in `current(principal)`, retains what arrives and only
then reads, so a change in a gap between streams is never lost. A notification says only that
something changed: `MemberStanding` is the whole standing, so after a lost, repeated or late one
the next read shows today's truth. A subscriber of `member://standing` also receives the URI of a
place window or view `current` holds; a subscriber of one place gets that place alone. The stream ends
after `LISTEN_BOUND_SECONDS` with a completion result carrying the subscription id; a stream cut
without one is `SubscriptionLost` at the SDK's client. After either the runner listens again, and
reads again once acknowledged. A listen needs an SSE `Accept`, else it is `406`. The SDK's
`Client.listen` holds no task group, so whatever the runner's own loop body raises arrives bare at
its `yield`: a runner that maps the stream's failures maps only the stream's own, which are the
opening, `SubscriptionLost` during iteration and its deadline.

**Revocation.** Every pulse is checked against `current` when it is drained from the stream's
pending URIs, so a removal ends delivery on an open stream, and `current` runs once per pulse, so
it must be cheap. The check is not at the socket: after a removal, up to two already-checked
URI-only frames can still reach a client that stalled, so a host relies on the read for
revocation, not on the absence of a pulse. A port that raises at the acknowledgement answers
`-32603` with one fixed text, one that raises later ends the stream with the completion, and both
log only the exception's type. Proved by `tests/test_mcp_server_listen.py`.

**Whether a member listens.** `changes.listens_to(principal, uri)` answers whether that principal
holds a stream whose acknowledgement honored the URI and which is open or ended less than
`LISTEN_ALLOWANCE_SECONDS` ago; an app that offers an agent only while it can answer an invitation
asks for `STANDING_URI`. The allowance lets a runner that listens again at once (the connection and
one `current` call, a few milliseconds plus the port's latency) keep the answer yes. The answer is
no before the acknowledgement, after the allowance of the last such stream and for a stream that
honored only a place window, because the URI is compared as asked and not widened to the places a
standing subscriber also receives. Several streams of one principal count while any of them
counts. It runs on the front's event loop, like `changed`.

The answer reads what the stream's acknowledgement honored; it does not re-check the member's
current resources, so a removal turns it no only once the stream has ended and the allowance has
passed. The standing is every admitted member's own resource, and whether the member still holds
its account and its places is the app's own record: an app combines the answer with that record.

`ResourceChanges(listening_changed)` takes one optional callable, `(principal, uri, listens) ->
None`, called when the answer for a principal and URI changes: with `True` on the request task
that wrote the first acknowledgement honoring the URI, and with `False` on the event loop once the
last such stream ended and the allowance passed. It must not block (a host schedules its own work
from it), and an exception from it is logged by type and never reaches a listen. The call is made
for every URI the acknowledgement honored, so a host picks the ones it offers on. The read and the
call come from one rule, so they never disagree. Proved by
`tests/test_mcp_server_listen_liveness.py`.

**The lease.** Every call a member makes is an admitted call, each opening of a stream included,
and the app renews the member's lease on each. A runner that calls again within `LEASE_SECONDS`
keeps its lease. The lease says nothing about listening: a call from an agent that listens
nowhere renews it too. The listen is the offer rule only; a request to call an agent and the
agent's accept keep the lease as their check.

**What each app owns:** its places and who stands where, the invitation, its expiry (it lasts
`INVITATION_SECONDS`) and the epoch check at accept, the participation and permission
decisions, and enforcing the lease, removing a member whose lease ran out.

## Hearing and speaking

A member that hears a conversation and speaks in it does both through the member module's
records, the same way in every app. The library carries text only: the app turns speech into text
and a sentence into sound.

**Hearing.** Heard speech is a window entry with `spoken` true: its `text` is one finished
utterance and its `name` the speaker's shown name.

**Speaking** is the side-effecting tool `SPEAK_TOOL`, `speak`. Its arguments are
`SpeakArguments(place, text)`: the `PlaceRef`, as for `accept_invitation`, and one sentence. On
success it answers `Spoken(duration_ms)` as JSON, the sound's length in milliseconds, at least 1.
A refusal is an `is_error` result whose content is `SpeakRefused(refused)` as JSON, such as
`{"refused": "too_long"}`, with one `SpeakRefusal` code:

- `not_active`: the member left, was removed or is paused;
- `too_long`: the sentence is longer than the app speaks at once;
- `unavailable`: the app cannot speak now.

Proved by `tests/test_member.py`.

**What each app that hears and speaks owns:**

- It appends finished utterances only, one entry per utterance, under the speaker's shown name
  and its own `kind`, and never provisional text.
- It enters a person's speech only while that person allows it under the app's own rule.
- It appends the member's own sentences as `own` and `spoken` when they start playing.
- It plays one member's sentences in order, each behind that member's previous sentence, and
  answers each with the sound's length.
- It bounds a sentence's length, refuses with these codes, and drops the queued sentences of a
  removed or paused member at once.
- It bounds its window as it does without speech.
- Its own privacy section states what it sends and its legal basis.


## A marked route as a tool

A host turns the routes of its FastAPI app into person-front tools. `tools.agent_tool(name=…,
effect=…)` is the mark and `tools.not_an_agent_tool(reason=…)` the exclusion, each passed as a
route's `openapi_extra` and each stored under the one extension key `x-agent-tool`, so a route
holds only one. The client imports no FastAPI.

**The rule.** Every operation of the document is decided: marked, or excluded with its reason. A
route becomes a tool only through its own mark; an exclusion yields no tool and no mark.
`route_tools` decides first, over the whole document: when any operation carries neither, it
raises one `RouteProfileError` naming every undecided operation in document order, before a tool
is built or `mark_route` runs. Only then is each operation judged: an exclusion whose reason is
not a non-blank string, or an extension value that neither function returns, is refused naming
its operation. An excluded operation is never judged against the profile below: its method,
parameters, body, responses and description may be anything, which is how an upload, a stream or
a browser-only route is left out. It still counts as another operation of its method in the
overlap check. Proved by
`test_an_operation_with_neither_mark_nor_exclusion_is_refused_naming_every_undecided_one`,
`test_an_undecided_operation_is_refused_even_when_a_marked_one_breaches_the_profile`,
`test_an_exclusion_that_not_an_agent_tool_does_not_return_is_refused` and
`test_an_excluded_route_outside_the_profile_is_accepted_beside_a_marked_tool`.
`mcp_server.route_tools(openapi, app, *, mark_route)` reads the host's own `app.openapi()` and
returns `RouteTools(tools, marks)`: the `PersonTool`s, and a `frozenset` of what
`mark_route(METHOD, template)` returned for each marked operation, in the host's own mark type.
`mark_route` runs only on marked operations, never on an excluded one; its `ValueError` becomes
a `RouteProfileError` naming the operation, with the host's error as its cause, and any other
exception propagates. Proved by `tests/test_mcp_server_routes.py`:
`test_a_marked_route_becomes_a_tool_with_its_name_description_and_input_schema`,
`test_an_excluded_route_is_never_passed_to_the_host_mark_function`,
`test_the_host_mark_functions_value_error_becomes_a_profile_error_with_it_as_cause` and
`test_another_exception_from_the_host_mark_function_propagates_unchanged`.

The host wires in this order: register every route, read `app.openapi()` (FastAPI caches it),
run `route_tools`, then build the gate with the marks.

**The profile** of a marked operation. `GET`, `POST`, `PUT`, `PATCH` and `DELETE` can be marked,
`HEAD`, `OPTIONS` and `TRACE` only excluded; a `PUT`, `PATCH` or `DELETE` marked `READ_ONLY` is
refused, because RFC 9110 §9.2.1 does not count them safe and a client may run a read-only tool
without asking. Parameters are string path parameters and scalar query parameters (string,
integer, number or boolean, with or without `enum`, inline or by `$ref`, nullable through `anyOf
[x, null]`); the one body is a required `application/json` object whose fields are flattened
beside the parameters; `$ref` is inlined from `components`, and an annotation beside a reference
is kept and wins over the component's own; every success response is JSON, and an error status may
also declare RFC 9457 problem details (`application/problem+json`), beside or instead of JSON. A
success without JSON content, such as a `204`, is refused: such a route is excluded or answers
JSON. The tool's description is the operation's `description` (the route's docstring) and is
required. Everything else is refused at start with `RouteProfileError`: a mark on `HEAD`,
`OPTIONS` or `TRACE`, a mark that is not the mapping `agent_tool` returns, header and cookie
parameters, a non-JSON or optional body, a success response that is not JSON, an error response
that is neither JSON nor problem details, a recursive schema, a body object carrying any keyword
flattening would lose, a `$ref` beside any keyword that is not an annotation, a name shared by two
inputs, two marks of one name, an integer path parameter and a missing description. Proved by
`test_a_put_patch_or_delete_marked_read_only_is_refused_naming_the_operation`,
`test_a_head_options_or_trace_route_may_be_excluded_beside_a_marked_tool`,
`test_a_marked_route_outside_the_profile_is_refused_naming_the_operation`,
`test_an_error_status_may_declare_problem_details`,
`test_a_hand_written_mark_that_agent_tool_does_not_return_is_refused`,
`test_a_body_object_keyword_that_flattening_would_lose_is_refused`,
`test_a_constraint_beside_a_reference_is_refused` and `test_two_marks_of_one_name_are_refused`.

**Overlap.** Two templates of one method overlap when they have the same number of segments and
every pair of segments at a position could match one string: two literals only when equal, and
any pair holding a parameter always. A marked template that overlaps any other operation of its
method is refused whatever the order, whether the other is marked or excluded; a partial
segment may refuse a pair that never collides, never the reverse. The document cannot show
converters, routes hidden from it, a `Mount`, a WebSocket route, a catch-all or a second
registration of one method and path. A route hidden from the document, a `Mount` or a WebSocket
route never appears in it, so it can become neither a tool nor a mark. A converter is stripped
from the template (`/files/{rest:path}` reads as `/files/{rest}`), and FastAPI folds a second
registration of one method and path into one operation, so the overlap check and the profile
cannot see either: the host proves that no marked route has a converter, a catch-all or a
second registration.
Which route falls into an excluded class (deleting for good, deleting in bulk, sharing,
publishing, sending out) is judged mark by mark in each host's review, not in code. Proved by
`test_a_marked_template_that_overlaps_another_operation_of_its_method_is_refused_in_either_order`
and `test_a_marked_template_that_cannot_collide_with_another_operation_is_accepted`.

**The call.** Each tool call is one in-process ASGI request into `app`, which must be the host's
whole application, so the request passes every middleware, whatever the method
(`test_a_marked_put_patch_or_delete_is_called_with_its_path_query_and_body_arguments`). An
argument the schema does not name, a path value that is missing, not a string, empty, `.`, `..`
or holding `/`, and an object or array query value fail the call before any inner request
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

## The voice loop

An app that hears a conversation and speaks in it runs the voice loop in its own process, under
the `voice` extra:

```python
from agents.voice.loop import run_voice_loop

await run_voice_loop(
    agent, backend, person, hearing, voice, wakes=wake_rule, context=context, report=report
)
```

- `agent` is the `AgentDefinition`, with a `voice` section; without one, `run_voice_loop` raises
  `ValueError` before it reads `hearing`. The system text is its prompt, then the library's
  protocol text (`voice.reply.PROTOCOL_TEXT`: the line format, the window note, plain spoken
  sentences, `[silent]`), then `context`, the app's pinned part.
- `backend` is the app's `AgentBackend`, and `person` the one `TurnPerson` the loop runs for,
  never a speaker: every decision is charged to that person's budget at the gateway.
- `hearing` is an `AsyncIterator[PlaceEntry]` in increasing `seq`; a new window generation is a
  new loop.
- `voice` is the app's `VoiceOut[Audio]`: `render(sentence)` returns the sound and neither plays
  nor queues it, and `play(audio)` returns when it has ended. A sound is heard only through its
  own `play`, and cancelling or refusing a `play` stops that sound at once. Either raises
  `VoiceRefused` when the app cannot speak now. The loop plays the sentences in order: `play` for
  a sentence starts only after it returned for the one before. While one sentence plays, the loop
  renders the next, never more than one ahead, so `render` may run while `play` does, and a
  render taking no longer than the sentence playing before it adds no pause between the two.
- `wakes` is the app's `WakeRule(line, *, first_after_answer)`, asked for every line that is
  neither the agent's own nor an echo. `first_after_answer` is true for the first such line heard
  after a decision that played at least one sentence ended, while no decision is in flight, and
  false for every other line, one heard while an answer plays included.
- `report` receives each `VoiceEvent`: `Woke`, `StayedSilent`, `ReplyUnreadable`,
  `SentenceStarted(text)`, `SentenceRefused(text)`, `DecisionFailed(kind, code, detail)`, whose
  `outcome` is the stop's `StopOutcome`, and `EchoDropped(text)`.

Proved by `tests/test_voice_loop.py`:
`test_a_spoken_line_that_wakes_sends_one_request_with_prompt_protocol_context_and_the_line`,
`test_sentences_play_in_order_and_the_first_starts_while_the_reply_is_still_written`,
`test_the_first_line_after_an_answer_that_played_is_the_one_marked_first_after_answer` and
`test_no_line_after_a_decision_that_played_nothing_is_marked_first_after_answer`; the missing
`voice` section by `test_an_agent_without_a_voice_section_is_refused_before_hearing_is_read` in
`tests/test_voice_window.py`; the render ahead by `tests/test_voice_render_ahead.py`:
`test_the_next_sentence_renders_once_written_while_the_one_before_it_plays`,
`test_a_sentence_two_ahead_renders_only_after_the_one_playing_has_played`,
`test_sentences_play_in_written_order_each_started_after_the_one_before_it_played` and
`test_a_render_taking_no_longer_than_the_sentence_playing_before_it_adds_no_pause`.

**The standard wake rule.** `agents.voice.wake.addressed_or_asked(names)` returns
a `WakeRule`, a cheap local check of which lines cost a decision. It wakes on a line that names
one of `names` as a whole word in any case, on a line that ends with `?` before its trailing
spaces and closing quotes, and on the first line after an answer that played. Another agent's
line wakes by the same rule, and a line that does not wake still enters the conversation.
`names` without a name or with an empty one raises `ValueError`. Proved by
`tests/test_voice_wake.py`:
`test_a_line_naming_one_of_the_names_as_a_whole_word_in_any_case_wakes`,
`test_a_line_ending_in_a_question_mark_wakes_and_one_asking_only_mid_line_does_not`,
`test_a_rule_without_a_name_or_with_an_empty_one_is_refused` and
`test_in_the_loop_a_named_asked_or_first_line_after_a_played_answer_wakes_and_all_are_heard`.

**The echo rule.** The agent's own voice, heard back through a person's microphone, arrives as
that person's spoken line; the loop drops it so the agent does not answer itself. A spoken line
that is not the agent's own is an echo when it has counted words and at least half of them are
counted words of the agent's latest answer. A counted word is split at whitespace, stripped of
the punctuation at its edges and still longer than two characters, and is compared in lower
case; a short reply such as "Ja, ok." therefore always enters, and a word that is only
punctuation counts on neither side. The latest answer is the most recent decision that
played at least one sentence; its words are those of every sentence that started playing, the
one playing now included, never a queued one, one rendered ahead or a refused one. An echo does not
enter the conversation, is not asked of `wakes`, leaves `first_after_answer` to the next line and is
reported as `EchoDropped(text)`. It keeps its place in `seq` order like the agent's own spoken line:
an echo out of order ends the loop like any line, and it never counts in the next line's
`missed_before`. A written line is never an echo. The rule is a text-level guard, so it misses an
echo mixed with more words of a person, and it drops what a person said when at least half of its
counted words are the latest answer's; `EchoDropped` lets an app count the drops.
Proved by `tests/test_voice_echo.py`:
`test_a_spoken_line_is_an_echo_when_half_its_long_words_are_words_of_the_answer`,
`test_a_word_counts_and_compares_without_the_punctuation_at_its_edges`,
`test_only_a_spoken_line_that_is_not_the_agents_own_can_be_an_echo`,
`test_a_spoken_line_repeating_the_answer_is_dropped_never_asked_and_never_sent`,
`test_an_echo_heard_while_the_next_decision_is_read_repeats_the_answer_before_it`,
`test_a_line_repeating_only_an_older_answer_enters_while_the_latest_one_plays`,
`test_a_line_repeating_only_an_older_answer_enters_while_the_next_decision_is_read`,
`test_a_written_line_with_the_answers_words_enters_and_is_asked`,
`test_a_short_spoken_reply_to_the_agents_question_wakes_it_as_the_first_line_after`,
`test_a_sentence_still_queued_does_not_count_while_the_one_before_it_plays`,
`test_a_sentence_still_rendering_does_not_count_and_the_one_played_before_it_does`,
`test_a_refused_sentence_does_not_count_and_the_one_played_before_it_does`,
`test_after_a_decision_that_played_nothing_the_answer_before_it_is_still_the_latest`,
`test_an_echo_that_breaks_its_order_ends_the_loop_closed_and_silent` and
`test_the_line_after_an_echo_counts_only_the_lines_never_heard_as_missed`, and by
`test_a_line_repeating_only_the_sentence_rendered_ahead_is_no_echo_and_enters` in
`tests/test_voice_render_ahead.py`.

**The conversation.** Every line enters one running conversation, but an echo and the agent's
own spoken line: the loop records what it played itself. Within the window below, a decision
sends the pairs decided so far and one user message with every line since the last decision, one
JSON object per line with `name`, `kind`, `own`, `spoken` and `text`, and `missed_before`, the
number of `seq` values skipped, after a gap. The assistant message is what played, or
`[silent]`. The conversation lives in memory while the loop runs and is stored nowhere. Proved by
`test_a_line_that_does_not_wake_sends_nothing_and_leads_the_next_decisions_message`,
`test_an_own_spoken_line_never_enters_and_an_own_written_line_enters_but_never_wakes`,
`test_a_gap_in_seq_marks_the_next_line_with_how_many_were_missed` and
`test_a_silent_reply_speaks_nothing_runs_its_tools_and_is_recorded_as_silent`.

**The window.** Each decision sends only a window of the conversation, so a call does not cost
more the longer a talk runs. Between drops the messages only grow, so a request's prefix changes
only at a drop. Dropping decided pairs frees a quarter of the window, so a talk whose decisions
are recorded drops once per quarter it grows, never once per line. Undecided lines that alone
exceed the window lose only what they must, down to the window, because freeing more would drop
more lines the agent never saw; so while decisions give their lines back (a failed turn, an
unreadable reply, a refusal before anything played), each of them drops again and rewrites the
prompt. The bound limits the bytes sent, never whether the agent answers, and what a decision
dropped stays dropped, even when its lines stay undecided.

| Bound | Value | Over it |
| --- | --- | --- |
| One decision's messages | the agent file's `windowBytes`, in UTF-8 bytes of the messages' texts, because the library cannot count a provider's tokens; the system text does not count and never goes | the oldest whole decided pairs go until a quarter of the window is free; then, while the undecided lines alone exceed the window, the oldest of them go down to it; the newest line always stays, even when it alone exceeds the window. From the first drop on, `voice.conversation.WINDOW_NOTE` opens the first user message |

Every line that is no echo enters the conversation, and a decided pair leaves the window only
after it was sent.
The oldest undecided lines a drop takes are the one exception: they may never reach any request, and
only `WINDOW_NOTE` tells the agent that earlier parts are no longer shown.

Proved by `tests/test_voice_window.py`:
`test_a_talk_within_the_window_drops_nothing_and_each_request_extends_the_last`,
`test_a_request_beyond_the_window_drops_the_oldest_whole_pairs_until_a_quarter_is_free`,
`test_a_long_talk_drops_once_per_quarter_of_the_window_it_grows_never_once_per_line`,
`test_lines_beyond_the_window_on_their_own_lose_their_oldest_and_keep_the_newest`,
`test_a_line_larger_than_the_window_still_goes_whole_and_the_agent_answers` and
`test_pairs_a_decision_dropped_stay_dropped_and_the_note_stays_when_it_played_nothing`; the
note's sentence in the protocol text by
`test_the_protocol_text_names_the_note_that_opens_a_window_with_earlier_parts_dropped` in
`tests/test_voice_reply.py`.

**The order.** At most one decision is in flight and one answer plays. A line that wakes
meanwhile is held, and once both are done exactly one decision follows. The loop has no cap and
no rate limit; the person's budget at the gateway bounds it. Proved by
`test_two_lines_heard_while_deciding_of_which_one_wakes_get_exactly_one_more_decision`,
`test_lines_heard_while_deciding_that_do_not_wake_start_nothing_and_lead_the_next`,
`test_a_waking_line_heard_while_an_answer_plays_is_decided_after_it_plays_uncut` and
`test_two_hundred_waking_lines_each_after_the_last_answer_get_two_hundred_decisions`.

**The reply** is read per round, and a tool call ends a round. Leading whitespace is skipped. A
round that opens with `[` is held until `]` closes it within 16 characters; the word between,
trimmed of spaces and folded to ASCII lower case, is the marker. `silent` speaks nothing of the
round and its tools still run; any other word, no `]` in reach or a round that ends first speaks
nothing of it and reports `ReplyUnreadable`. Any other round is speech, a `[` inside it included:
a sentence is released once content follows it, a number and its period read as a German
ordinal, and the rest when the round ends. Proved by `tests/test_voice_reply.py`:
`test_a_rounds_opening_marker_decides_whether_any_of_it_is_spoken`,
`test_a_tool_call_ends_a_round_and_the_next_round_is_read_afresh`,
`test_a_sentence_is_released_only_once_content_follows_it` and
`test_an_answer_cut_anywhere_releases_each_sentence_once_whole_and_after_content`, by
`test_a_tool_rounds_text_plays_and_its_rest_is_released_when_the_round_ends` in
`tests/test_voice_loop.py` and by `test_text_splits_into_sentences_and_an_ordinal_does_not_end_one`
in `tests/test_voice_sentences.py`.

**Failures.**

- `VoiceRefused` ends the answer: the turn is closed, the queued sentences are dropped, a
  rendered one included, and `SentenceRefused` is reported; a render refused while the sentence
  before it plays is reported once that one has played. Proved by
  `test_a_refused_render_ends_the_answer_closes_the_turn_and_keeps_what_played`,
  `test_a_render_refused_while_the_one_before_it_plays_is_reported_after_that_one_played` and
  `test_a_refused_play_never_plays_starts_or_records_the_sentence_rendered_after_it`.
- `TurnStopped` is reported as `DecisionFailed`; the sentences already queued play out and the
  held rest is dropped. Proved by
  `test_a_turn_stopped_after_a_sentence_played_plays_out_the_queued_one_and_drops_the_rest` and
  `test_a_turn_stopped_while_a_sentence_plays_still_plays_the_one_rendered_after_it`.
- A decision that played nothing and was not silent leaves its lines undecided, and only a new
  waking line starts the next decision, so a spent budget never makes the loop retry on its own.
  Proved by `test_a_decision_that_played_nothing_leaves_its_lines_undecided_until_a_line_wakes`.
- The turn is read to its end in its own task while the answer plays, so `maxDurationSeconds`
  bounds the writing, never the playing. Proved by
  `test_playing_longer_than_the_turns_limit_plays_every_sentence_without_a_limit_failure`.
- When hearing ends, the loop closes the turn, stops the sound and returns. Any other error of a
  port or the backend, and a `seq` not above the last, closes the turn, stops the sound and is
  raised inside an `ExceptionGroup`. On the app's cancel `CancelledError` propagates, the sound
  stops, the render ahead is cancelled and nothing more is reported. Proved by
  `test_when_hearing_ends_the_loop_closes_the_turn_stops_the_sound_and_returns`,
  `test_a_backend_error_other_than_a_stopped_turn_is_raised`,
  `test_a_hearing_that_breaks_its_order_or_raises_ends_the_loop_closed_and_silent`,
  `test_a_voice_that_raises_another_error_closes_the_turn_and_raises_it`,
  `test_the_hosts_cancel_closes_the_turn_stops_the_sound_and_reports_nothing_more` and
  `test_the_hosts_cancel_stops_the_sentence_playing_and_the_render_after_it`.
- A port that never returns holds the answer while hearing goes on; the app bounds its own ports.
  Proved by
  `test_a_voice_that_never_returns_still_hears_lines_but_starts_no_decision_until_cancelled`.
