"""The person front: the host's resolver runs first, then the head checks and revision 2026-07-28.

Every refusal case also asserts that no handler ran.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp_types import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from starlette.testclient import TestClient

from agents.mcp_server import McpPersonFront, PersonRequest, PersonTool
from agents.mcp_server.asgi import LOGGER_NAME as LOGGER
from agents.mcp_wire import MCP_ENDPOINT_PATH, PROTOCOL_VERSION
from agents.tools import Principal, ToolEffect, ToolOutcome
from agents.turn import MAX_EVENT_BYTES, MAX_TOOL_INPUT_DEPTH, TOOL_IMAGE_MEDIA_TYPE, JsonObject
from tests.support.mcp_server_support import (
    Headers,
    call_params,
    declaration,
    drive,
    http_scope,
    routing_headers,
    rpc_body,
    sdk_client,
    served_app,
)
from tests.support.tool_images import a_tool_image

_MARKER = "credential-marker-7f3a"


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Headers
    body: bytes

    def header(self, name: str) -> list[str]:
        return [v.decode() for k, v in self.headers if k == name.encode()]

    def json(self) -> Any:
        return json.loads(self.body)


def reply_of(sent: list[dict[str, Any]]) -> Reply:
    [start] = [m for m in sent if m["type"] == "http.response.start"]
    return Reply(start["status"], start["headers"], b"".join(m.get("body", b"") for m in sent))


async def drive_scope(front: McpPersonFront, scope: dict[str, Any], body: bytes) -> Reply:
    async with front.running():
        return reply_of(await drive(front, scope, body))


@dataclass
class Person:
    """The host side: a resolver that reads the caller off a header, and recording tools."""

    resolved: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, PersonRequest, JsonObject]] = field(default_factory=list)
    outcome: ToolOutcome = ToolOutcome("done", is_error=False)
    failure: Exception | None = None

    def resolve(self, scope: dict[str, Any]) -> Principal:
        self.resolved.append(scope)
        headers = dict(scope["headers"])
        if b"x-refused" in headers:
            raise LookupError("the gate admitted nobody")
        return Principal(iss="https://issuer.test", sub=headers.get(b"x-person", b"alice").decode())

    def tool(self, name: str, effect: ToolEffect = ToolEffect.READ_ONLY) -> PersonTool:
        async def handler(request: PersonRequest, arguments: JsonObject) -> ToolOutcome:
            await asyncio.sleep(0)
            self.calls.append((name, request, arguments))
            if self.failure is not None:
                raise self.failure
            return self.outcome

        return PersonTool(declaration(name, effect), handler)


@pytest.fixture
def person() -> Person:
    return Person()


@pytest.fixture
def front(person: Person) -> McpPersonFront:
    tools = [person.tool("read_note"), person.tool("edit_note", ToolEffect.SIDE_EFFECTING)]
    return McpPersonFront(tools, resolve_person=person.resolve)


def headers_of(
    body: bytes, *extra: tuple[bytes, bytes], version: str | None = PROTOCOL_VERSION
) -> Headers:
    return [(b"host", b"notes.internal"), *extra, *routing_headers(body, version)]


def post(front: McpPersonFront, body: bytes, *extra: tuple[bytes, bytes], **options: Any) -> Reply:
    version = options.pop("version", PROTOCOL_VERSION)
    scope = http_scope(headers_of(body, *extra, version=version), **options)
    return asyncio.run(drive_scope(front, scope, body))


def test_a_resolver_failure_reaches_the_host_before_the_origin_check_and_the_body(
    front: McpPersonFront, person: Person
) -> None:
    received: list[str] = []
    sent: list[Any] = []

    async def receive() -> dict[str, Any]:
        received.append("read")
        return {"type": "http.request", "body": rpc_body("ping")}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = http_scope(headers_of(b"", (b"x-refused", b"1"), (b"origin", b"https://evil.test")))
    running = front(scope, receive, send)

    with pytest.raises(LookupError):
        asyncio.run(running)
    assert (received, sent, person.calls) == ([], [], [])


@pytest.mark.parametrize(
    "result", [lambda: None, lambda: asyncio.sleep(0)], ids=["none", "coroutine"]
)
def test_a_resolver_result_that_is_not_a_principal_reaches_the_host_before_the_body(
    person: Person, result: Callable[[], Any]
) -> None:
    received: list[str] = []
    sent: list[Any] = []
    produced: list[Any] = []

    def resolve(_scope: dict[str, Any]) -> Any:
        produced.append(result())
        return produced[-1]

    async def receive() -> dict[str, Any]:
        received.append("read")
        return {"type": "http.request", "body": rpc_body("ping")}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    front = McpPersonFront([person.tool("read_note")], resolve_person=resolve)
    running = front(http_scope(headers_of(b"")), receive, send)

    with pytest.raises(TypeError):
        asyncio.run(running)
    for item in produced:
        if asyncio.iscoroutine(item):
            item.close()
    assert (received, sent, person.calls) == ([], [], [])


@pytest.mark.parametrize("origin", [b"https://evil.test", b"null", b""])
def test_a_request_carrying_an_origin_of_any_value_is_forbidden_and_runs_nothing(
    front: McpPersonFront, person: Person, origin: bytes
) -> None:
    reply = post(front, rpc_body("tools/call", call_params("read_note")), (b"origin", origin))

    assert (reply.status, reply.body, person.calls) == (403, b"", [])


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
def test_anything_but_post_is_method_not_allowed_with_the_allowed_method_named(
    front: McpPersonFront, person: Person, method: str
) -> None:
    reply = post(front, b"", method=method)

    assert (reply.status, reply.header("allow"), person.calls) == (405, ["POST"], [])


def test_a_body_one_byte_over_the_bound_is_too_large_and_runs_nothing(
    front: McpPersonFront, person: Person
) -> None:
    reply = post(front, b" " * (MAX_EVENT_BYTES + 1))

    assert (reply.status, person.calls) == (413, [])


def _listing_with_id(raw_id: bytes) -> bytes:
    return rpc_body("tools/list").replace(b'"id": 1', b'"id": ' + raw_id, 1)


@pytest.mark.parametrize(
    ("body", "version", "answered_id"),
    [
        (rpc_body("tools/list"), None, 1),
        (rpc_body("tools/list"), "2024-11-05", 1),
        (rpc_body("tools/list"), "2025-03-26", 1),
        (rpc_body("tools/list"), "2025-06-18", 1),
        (rpc_body("tools/list"), "2025-11-25", 1),
        (rpc_body("tools/list"), "1999-01-01", 1),
        (rpc_body("tools/call", call_params("read_note")), None, 1),
        (rpc_body("initialize", {"capabilities": {}}, enveloped=False), None, 1),
        (rpc_body("initialize", {"capabilities": {}}, enveloped=False), PROTOCOL_VERSION, 1),
        (_listing_with_id(b"true"), None, None),
        (_listing_with_id(b"true"), "2020-01-01", None),
        (_listing_with_id(rb'"lone \ud800"'), None, None),
        (_listing_with_id(rb'"lone \ud800"'), "2020-01-01", None),
    ],
    ids=[
        "list absent",
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
        "unknown",
        "call absent",
        "initialize absent",
        "initialize at the revision",
        "boolean id absent",
        "boolean id unsupported",
        "lone surrogate id absent",
        "lone surrogate id unsupported",
    ],
)
def test_anything_but_a_request_at_the_served_revision_is_refused_naming_it(
    front: McpPersonFront,
    person: Person,
    body: bytes,
    version: str | None,
    answered_id: int | None,
) -> None:
    reply = post(front, body, version=version)

    error = reply.json()["error"]
    assert (reply.status, reply.json()["id"]) == (400, answered_id)
    assert (error["code"], error["data"]["supported"]) == (
        UNSUPPORTED_PROTOCOL_VERSION,
        [PROTOCOL_VERSION],
    )
    assert person.calls == []


def _note_call_with(*, once: bytes, doubled: bytes) -> bytes:
    body = rpc_body("tools/call", call_params("read_note", note="n1"))
    assert once in body
    return body.replace(once, doubled, 1)


@pytest.mark.parametrize(
    "body",
    [
        _note_call_with(once=b'"id": 1', doubled=b'"id": 1, "id": 2'),
        _note_call_with(
            once=b'"name": "read_note"',
            doubled=b'"name": "read_note", "name": "read_note"',
        ),
        _note_call_with(once=b'"_meta": {', doubled=b'"_meta": {"x": 1, "x": 2, '),
        _note_call_with(once=b'"n1"', doubled=b"NaN"),
        _note_call_with(once=b'"n1"', doubled=b"Infinity"),
        _note_call_with(once=b'"n1"', doubled=b"1e999"),
        b"{truncated",
    ],
    ids=[
        "repeated id",
        "repeated in params",
        "repeated in meta",
        "NaN",
        "Infinity",
        "overflowing number",
        "not JSON",
    ],
)
def test_a_body_with_a_repeated_member_or_a_non_json_number_is_refused_before_dispatch(
    front: McpPersonFront, person: Person, body: bytes
) -> None:
    reply = post(front, body)

    assert (reply.status, reply.json()["error"]["code"], person.calls) == (400, PARSE_ERROR, [])


@pytest.mark.parametrize(
    ("body", "changed", "status", "code"),
    [
        (rpc_body("tools/list"), (b"mcp-method", b"tools/call"), 400, HEADER_MISMATCH),
        (rpc_body("tools/list", enveloped=False), None, 400, INVALID_PARAMS),
        (rpc_body("no/such/method"), None, 404, METHOD_NOT_FOUND),
    ],
    ids=["header mismatch", "missing meta", "unknown method"],
)
def test_the_sdk_ladder_answers_a_malformed_request_with_its_standard_error(
    front: McpPersonFront,
    person: Person,
    body: bytes,
    changed: tuple[bytes, bytes] | None,
    status: int,
    code: int,
) -> None:
    headers = headers_of(body)
    if changed is not None:
        headers = [(k, v) for k, v in headers if k != changed[0]] + [changed]

    reply = asyncio.run(drive_scope(front, http_scope(headers), body))

    assert (reply.status, reply.json()["error"]["code"], person.calls) == (status, code, [])


def test_tools_list_shows_every_tool_with_the_read_only_hint_only_on_a_read_only_one(
    front: McpPersonFront,
) -> None:
    listed = post(front, rpc_body("tools/list")).json()["result"]["tools"]

    assert [(tool["name"], tool.get("annotations")) for tool in listed] == [
        ("read_note", {"readOnlyHint": True}),
        ("edit_note", None),
    ]


def test_a_call_hands_the_handler_the_resolved_person_and_the_request_scope(
    front: McpPersonFront, person: Person
) -> None:
    reply = post(front, rpc_body("tools/call", call_params("read_note", note="n1")))

    [(name, request, arguments)] = person.calls
    assert reply.json()["result"]["content"] == [{"type": "text", "text": "done"}]
    assert (name, request.principal.sub, arguments) == ("read_note", "alice", {"note": "n1"})
    assert request.scope is person.resolved[0]


def _arguments_nested_to_depth(depth: int) -> dict[str, Any]:
    """Arguments whose object and the arrays and objects inside it make ``depth`` containers."""
    nested: list[Any] = []
    for _ in range(depth - 2):
        nested = [nested]
    return {"note": nested}


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "note": "n1",
            "tags": ["a", ["b", {"deep": [1, 2.5, None, True]}]],
            "meta": {"empty": {}, "list": [], "text": "caf\u00e9"},
        },
        _arguments_nested_to_depth(MAX_TOOL_INPUT_DEPTH),
    ],
    ids=["nested values", "nested exactly to the depth limit"],
)
def test_a_call_hands_the_handler_exactly_the_parsed_nested_arguments(
    front: McpPersonFront, person: Person, arguments: dict[str, Any]
) -> None:
    reply = post(front, rpc_body("tools/call", call_params("read_note", **arguments)))

    [(_, _, received)] = person.calls
    assert reply.json()["result"]["content"] == [{"type": "text", "text": "done"}]
    assert received == arguments


def test_concurrent_calls_each_get_the_person_and_the_scope_of_their_own_request(
    front: McpPersonFront, person: Person
) -> None:
    async def both() -> None:
        body = rpc_body("tools/call", call_params("read_note"))
        scopes = [
            http_scope(headers_of(body, (b"x-person", name.encode()))) | {"marker": name}
            for name in ("alice", "bob")
        ]
        async with front.running():
            await asyncio.gather(*(drive(front, scope, body) for scope in scopes))

    asyncio.run(both())

    assert sorted(
        (request.principal.sub, request.scope["marker"]) for _, request, _ in person.calls
    ) == [
        ("alice", "alice"),
        ("bob", "bob"),
    ]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("ghost_tool", {}),
        ("read_note", {"note": "lone \ud800"}),
        ("read_note", {"lone \ud800": "note"}),
        ("read_note", _arguments_nested_to_depth(MAX_TOOL_INPUT_DEPTH + 1)),
    ],
    ids=["unknown tool", "lone surrogate value", "lone surrogate key", "nested past the depth"],
)
def test_a_call_no_handler_may_receive_is_invalid_params_and_runs_nothing(
    front: McpPersonFront, person: Person, name: str, arguments: dict[str, Any]
) -> None:
    reply = post(front, rpc_body("tools/call", {"name": name, "arguments": arguments}))

    error = reply.json()["error"]
    assert (reply.status, error["code"], person.calls) == (400, INVALID_PARAMS, [])
    assert "ud800" not in reply.body.decode()
    assert "data" not in error


def test_two_tools_failing_differently_answer_one_failed_result_without_their_messages(
    front: McpPersonFront, person: Person, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    failing_calls = [
        ("read_note", RuntimeError("first secret")),
        ("edit_note", KeyError("second secret")),
    ]
    results = []
    for tool_name, failure in failing_calls:
        person.failure = failure
        results.append(post(front, rpc_body("tools/call", call_params(tool_name))).json()["result"])

    assert results[0] == results[1]
    assert results[0]["isError"] is True
    assert results[0]["content"][0]["text"] != ""
    logged = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert not any("secret" in message for _, message in logged)
    assert not any("secret" in json.dumps(result) for result in results)
    assert any(
        level == "ERROR" and "edit_note" in text and "KeyError" in text for level, text in logged
    )


@pytest.mark.parametrize(
    "spelling",
    ["Bearer {}", "bearer {}", "BEARER {}", "  Bearer {}  ", "\tbearer {}\t", "Bearer  {}"],
)
def test_a_result_carrying_the_request_bearer_is_withheld_and_the_log_names_no_caller_text(
    front: McpPersonFront, person: Person, caplog: pytest.LogCaptureFixture, spelling: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    token = "head.payload.sigOfTheRequest"
    person.outcome = ToolOutcome(f"debug: {token}", is_error=False)

    reply = post(
        front,
        rpc_body("tools/call", call_params("read_note", note="caller text")),
        (b"authorization", spelling.format(token).encode()),
    )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert (reply.status, reply.body) == (500, b"")
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert not any(word in logged for word in ("sigOfTheRequest", "caller text"))


def test_a_handler_rewriting_the_scope_headers_cannot_keep_the_request_bearer_in_its_answer(
    person: Person,
) -> None:
    token = "head.payload.sigOfTheRequest"

    async def handler(request: PersonRequest, arguments: JsonObject) -> ToolOutcome:
        request.scope["headers"] = []
        return ToolOutcome(f"debug: {token}", is_error=False)

    front = McpPersonFront(
        [PersonTool(declaration("read_note", ToolEffect.READ_ONLY), handler)],
        resolve_person=person.resolve,
    )

    reply = post(
        front,
        rpc_body("tools/call", call_params("read_note")),
        (b"authorization", f"Bearer {token}".encode()),
    )

    assert (reply.status, reply.body) == (500, b"")


def test_a_result_carrying_another_tokens_signature_is_answered_as_usual(
    front: McpPersonFront, person: Person
) -> None:
    person.outcome = ToolOutcome("debug: head.payload.sigOfAnother", is_error=False)

    reply = post(
        front,
        rpc_body("tools/call", call_params("read_note")),
        (b"authorization", b"Bearer head.payload.sigOfTheRequest"),
    )

    assert reply.status == 200
    assert "sigOfAnother" in reply.json()["result"]["content"][0]["text"]


def test_an_outcome_that_cannot_be_written_answers_the_failed_result_and_leaks_nothing(
    front: McpPersonFront, person: Person
) -> None:
    person.outcome = ToolOutcome("lone \ud800 surrogate", is_error=False)
    person.failure = None
    unwritable = post(front, rpc_body("tools/call", call_params("read_note"))).json()["result"]
    person.outcome = ToolOutcome("done", is_error=False)
    person.failure = RuntimeError("x")
    raised = post(front, rpc_body("tools/call", call_params("read_note"))).json()["result"]

    assert unwritable == raised


@pytest.mark.parametrize("is_error", [False, True])
def test_one_info_record_names_the_tool_and_whether_the_call_failed(
    front: McpPersonFront, person: Person, caplog: pytest.LogCaptureFixture, is_error: bool
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    person.outcome = ToolOutcome("text", is_error=is_error)

    post(front, rpc_body("tools/call", call_params("read_note")))

    [record] = [r for r in caplog.records if r.levelno == logging.INFO]
    assert ("read_note" in record.getMessage(), str(is_error) in record.getMessage()) == (
        True,
        True,
    )


def test_two_tools_of_one_name_are_refused_at_construction(person: Person) -> None:
    tools = [person.tool("read_note"), person.tool("read_note")]

    with pytest.raises(ValueError, match="more than once"):
        McpPersonFront(tools, resolve_person=person.resolve)


def test_an_exact_starlette_route_serves_discovery_list_and_call_without_a_redirect(
    front: McpPersonFront,
) -> None:
    def exchange(client: TestClient, body: bytes) -> Any:
        headers = {name.decode(): value.decode() for name, value in routing_headers(body)}
        return client.post(MCP_ENDPOINT_PATH, content=body, headers=headers)

    with TestClient(
        served_app(front), base_url="http://notes.internal", follow_redirects=False
    ) as client:
        discovered = exchange(client, rpc_body("server/discover"))
        listed = exchange(client, rpc_body("tools/list"))
        called = exchange(client, rpc_body("tools/call", call_params("read_note")))

    assert [r.status_code for r in (discovered, listed, called)] == [200, 200, 200]
    assert json.loads(called.content)["result"]["isError"] is False


def test_the_sdk_client_discovers_the_served_revision_and_a_capability_of_tools_alone(
    front: McpPersonFront,
) -> None:
    async def discovered() -> tuple[str, dict[str, Any]]:
        async with sdk_client(front) as client:
            capabilities = client.server_capabilities.model_dump(exclude_none=True)
            return client.protocol_version, capabilities

    version, capabilities = asyncio.run(discovered())

    assert version == PROTOCOL_VERSION
    assert list(capabilities) == ["tools"]


def test_the_sdk_client_lists_and_calls_a_tool(front: McpPersonFront, person: Person) -> None:
    async def listed_and_called() -> Any:
        async with sdk_client(front) as client:
            return await client.list_tools(), await client.call_tool("read_note", {"note": "n1"})

    listed, called = asyncio.run(listed_and_called())

    assert [tool.name for tool in listed.tools] == ["read_note", "edit_note"]
    assert (called.is_error, called.content[0].text) == (False, "done")
    assert [arguments for _, _, arguments in person.calls] == [{"note": "n1"}]


def test_the_sdk_client_gets_an_image_result_as_an_image_block_after_its_text(
    front: McpPersonFront, person: Person
) -> None:
    image = a_tool_image()
    person.outcome = ToolOutcome("a doorway", is_error=False, image=image)

    async def called() -> Any:
        async with sdk_client(front) as client:
            return await client.call_tool("read_note", {})

    result = asyncio.run(called())

    text_block, image_block = result.content
    assert (result.is_error, text_block.text) == (False, "a doorway")
    assert image_block.mime_type == TOOL_IMAGE_MEDIA_TYPE
    assert base64.b64decode(image_block.data, validate=True) == image.content


@pytest.mark.parametrize(
    ("image_bytes", "text_bytes", "reaches_the_client"),
    [
        (MAX_EVENT_BYTES - 10, 10, True),
        (MAX_EVENT_BYTES - 10, 11, False),
        (MAX_EVENT_BYTES, 0, True),
        (MAX_EVENT_BYTES + 1, 0, False),
    ],
    ids=[
        "text and image at the bound",
        "text and image one over",
        "an image alone at the bound",
        "an image alone one over",
    ],
)
def test_an_image_result_over_the_event_bound_is_replaced_by_a_fixed_error(
    front: McpPersonFront,
    person: Person,
    image_bytes: int,
    text_bytes: int,
    reaches_the_client: bool,
) -> None:
    person.outcome = ToolOutcome("a" * text_bytes, is_error=False, image=a_tool_image(image_bytes))

    result = post(front, rpc_body("tools/call", call_params("read_note"))).json()["result"]

    assert [block["type"] for block in result["content"]] == (
        ["text", "image"] if reaches_the_client else ["text"]
    )
    assert result["isError"] is (not reaches_the_client)


def test_a_failing_tool_gets_the_fixed_text_through_the_sdk_client(
    front: McpPersonFront, person: Person
) -> None:
    async def called() -> Any:
        async with sdk_client(front) as client:
            return await client.call_tool("read_note", {})

    person.failure = RuntimeError("secret detail")

    result = asyncio.run(called())

    assert result.is_error is True
    assert result.content[0].text == "the tool failed"


@pytest.mark.parametrize("entered_before", [False, True], ids=["never entered", "already exited"])
def test_a_request_outside_running_fails_loud(front: McpPersonFront, entered_before: bool) -> None:
    async def outside() -> None:
        if entered_before:
            async with front.running():
                pass
        body = rpc_body("tools/list")
        await drive(front, http_scope(headers_of(body)), body)

    request = outside()

    with pytest.raises(RuntimeError, match="running"):
        asyncio.run(request)


def test_a_front_running_already_refuses_a_second_entry(front: McpPersonFront) -> None:
    async def overlapping() -> None:
        async with front.running(), front.running():
            pass

    entered = overlapping()

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(entered)
    assert raised.group_contains(RuntimeError, match="already running")


def test_a_front_serves_again_after_its_lifespan_ended(front: McpPersonFront) -> None:
    async def twice() -> list[Reply]:
        body = rpc_body("tools/list")
        replies = []
        for _ in range(2):
            async with front.running():
                replies.append(reply_of(await drive(front, http_scope(headers_of(body)), body)))
        return replies

    assert [reply.status for reply in asyncio.run(twice())] == [200, 200]


@pytest.fixture
def unset_sdk_loggers() -> Iterator[dict[str, logging.Logger]]:
    """The SDK's server loggers as a host that configured neither leaves them, restored after."""
    loggers = {name: logging.getLogger(name) for name in ("mcp.server", "mcp.shared")}
    levels = {name: logger.level for name, logger in loggers.items()}
    for logger in loggers.values():
        logger.setLevel(logging.NOTSET)
    yield loggers
    for name, logger in loggers.items():
        logger.setLevel(levels[name])


@pytest.mark.parametrize(
    ("host_level", "critical_reaches_a_handler"),
    [(logging.NOTSET, False), (logging.DEBUG, True)],
    ids=["left unset is held", "set by the host wins"],
)
def test_running_holds_only_an_sdk_logger_the_host_left_unset(
    front: McpPersonFront,
    unset_sdk_loggers: dict[str, logging.Logger],
    host_level: int,
    critical_reaches_a_handler: bool,
) -> None:
    logger = unset_sdk_loggers["mcp.server"]
    logger.setLevel(host_level)

    async def enabled_inside_running() -> bool:
        async with front.running():
            return logger.isEnabledFor(logging.CRITICAL)

    assert asyncio.run(enabled_inside_running()) is critical_reaches_a_handler


@dataclass(frozen=True)
class MarkedRequest:
    """One way a credential marker reaches the front, and what the host's tool does with it."""

    body: bytes
    failure: Exception | None = None
    outcome: Any = None
    version: str = PROTOCOL_VERSION


def _read_note() -> bytes:
    return rpc_body("tools/call", call_params("read_note"))


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO], ids=["debug", "normal"])
@pytest.mark.parametrize(
    "marked",
    [
        pytest.param(
            lambda: MarkedRequest(_read_note(), failure=RuntimeError(_MARKER)),
            id="raising handler",
        ),
        pytest.param(
            lambda: MarkedRequest(
                _read_note(), outcome=ToolOutcome(f"{_MARKER} \ud800", is_error=False)
            ),
            id="unwritable outcome",
        ),
        pytest.param(
            lambda: MarkedRequest(_read_note(), outcome={"marker": _MARKER}),
            id="malformed outcome",
        ),
        pytest.param(lambda: MarkedRequest(rpc_body(f"tools/{_MARKER}")), id="request method"),
        pytest.param(
            lambda: MarkedRequest(rpc_body(f"notifications/{_MARKER}", notification=True)),
            id="notification method",
        ),
        pytest.param(
            lambda: MarkedRequest(rpc_body("tools/call", call_params("read_note", note=_MARKER))),
            id="tool arguments",
        ),
        pytest.param(
            lambda: MarkedRequest(rpc_body("tools/call", call_params(_MARKER))),
            id="unknown tool name",
        ),
        pytest.param(
            lambda: MarkedRequest(rpc_body("tools/list"), version=_MARKER), id="version header"
        ),
    ],
)
@pytest.mark.usefixtures("unset_sdk_loggers")
def test_no_credential_marker_reaches_any_log_under_debug_or_normal_logging(
    person: Person,
    front: McpPersonFront,
    caplog: pytest.LogCaptureFixture,
    marked: Callable[[], MarkedRequest],
    level: int,
) -> None:
    request = marked()
    person.failure = request.failure
    person.outcome = request.outcome or person.outcome
    caplog.set_level(level)

    post(front, request.body, version=request.version)

    logged = [
        (record.name, record.getMessage(), str(record.exc_info), str(record.stack_info))
        for record in caplog.records
    ]
    assert not any(_MARKER in " ".join(fields) for fields in logged), logged
    assert _MARKER not in caplog.text
