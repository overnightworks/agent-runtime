"""The person front: the host's resolver runs first, then the head checks and the profile.

Every refusal case also asserts that no handler ran.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_runtime_client.mcp_server import McpPersonFront, PersonRequest, PersonTool
from agent_runtime_client.mcp_server.asgi import LOGGER_NAME as LOGGER
from agent_runtime_client.mcp_wire import (
    LATEST_PROTOCOL_VERSION,
    MCP_ENDPOINT_PATH,
    JsonRpcErrorCode,
    ToolOutcome,
)
from agent_runtime_client.tools import Principal, ToolEffect
from agent_runtime_client.wire import MAX_EVENT_BYTES, JsonObject
from tests.support.mcp_server_support import declaration

VERSION = str(LATEST_PROTOCOL_VERSION)
Headers = list[tuple[bytes, bytes]]


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Headers
    body: bytes

    def header(self, name: str) -> list[str]:
        return [v.decode() for k, v in self.headers if k == name.encode()]

    def json(self) -> Any:
        return json.loads(self.body)


def rpc(method: str, params: dict[str, Any] | None = None, *, notification: bool = False) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if not notification:
        message["id"] = 1
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def call_params(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def http_scope(headers: Headers, *, method: str = "POST") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": MCP_ENDPOINT_PATH,
        "query_string": b"",
        "headers": headers,
    }


async def drive_scope(front: McpPersonFront, scope: dict[str, Any], body: bytes) -> Reply:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await front(scope, receive, send)
    [start] = [m for m in sent if m["type"] == "http.response.start"]
    return Reply(start["status"], start["headers"], b"".join(m.get("body", b"") for m in sent))


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


def headers_of(*extra: tuple[bytes, bytes], version: str | None = VERSION) -> Headers:
    fields = [(b"host", b"notes.internal"), *extra]
    return fields + [(b"mcp-protocol-version", version.encode())] if version else fields


def post(front: McpPersonFront, body: bytes, *extra: tuple[bytes, bytes], **options: Any) -> Reply:
    version = options.pop("version", VERSION)
    scope = http_scope(headers_of(*extra, version=version), **options)
    return asyncio.run(drive_scope(front, scope, body))


def test_a_resolver_failure_reaches_the_host_before_the_origin_check_and_the_body(
    front: McpPersonFront, person: Person
) -> None:
    received: list[str] = []
    sent: list[Any] = []

    async def receive() -> dict[str, Any]:
        received.append("read")
        return {"type": "http.request", "body": rpc("ping")}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = http_scope(headers_of((b"x-refused", b"1"), (b"origin", b"https://evil.test")))
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
        return {"type": "http.request", "body": rpc("ping")}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    front = McpPersonFront([person.tool("read_note")], resolve_person=resolve)
    running = front(http_scope(headers_of()), receive, send)

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
    reply = post(front, rpc("tools/call", call_params("read_note")), (b"origin", origin))

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


@pytest.mark.parametrize("version", [None, "1999-01-01"])
def test_a_missing_or_unknown_protocol_version_is_a_bad_request(
    front: McpPersonFront, person: Person, version: str | None
) -> None:
    reply = post(front, rpc("tools/list"), version=version)

    assert (reply.status, person.calls) == (400, [])


def test_tools_list_shows_every_tool_with_the_read_only_hint_only_on_a_read_only_one(
    front: McpPersonFront,
) -> None:
    listed = post(front, rpc("tools/list")).json()["result"]["tools"]

    assert [(tool["name"], tool.get("annotations")) for tool in listed] == [
        ("read_note", {"readOnlyHint": True}),
        ("edit_note", None),
    ]


def test_a_call_hands_the_handler_the_resolved_person_and_the_request_scope(
    front: McpPersonFront, person: Person
) -> None:
    reply = post(front, rpc("tools/call", call_params("read_note", note="n1")))

    [(name, request, arguments)] = person.calls
    assert reply.json()["result"]["content"] == [{"type": "text", "text": "done"}]
    assert (name, request.principal.sub, arguments) == ("read_note", "alice", {"note": "n1"})
    assert request.scope is person.resolved[0]


def test_concurrent_calls_each_get_the_person_and_the_scope_of_their_own_request(
    front: McpPersonFront, person: Person
) -> None:
    async def both() -> None:
        scopes = [
            http_scope(headers_of((b"x-person", name.encode()))) | {"marker": name}
            for name in ("alice", "bob")
        ]
        body = rpc("tools/call", call_params("read_note"))
        await asyncio.gather(*(drive_scope(front, scope, body) for scope in scopes))

    asyncio.run(both())

    assert sorted(
        (request.principal.sub, request.scope["marker"]) for _, request, _ in person.calls
    ) == [
        ("alice", "alice"),
        ("bob", "bob"),
    ]


def test_an_unknown_tool_is_invalid_params_and_runs_nothing(
    front: McpPersonFront, person: Person
) -> None:
    reply = post(front, rpc("tools/call", call_params("ghost_tool")))

    assert (reply.status, reply.json()["error"]["code"], person.calls) == (
        200,
        JsonRpcErrorCode.INVALID_PARAMS,
        [],
    )


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
        results.append(post(front, rpc("tools/call", call_params(tool_name))).json()["result"])

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
        rpc("tools/call", call_params("read_note", note="caller text")),
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
        rpc("tools/call", call_params("read_note")),
        (b"authorization", f"Bearer {token}".encode()),
    )

    assert (reply.status, reply.body) == (500, b"")


def test_a_result_carrying_another_tokens_signature_is_answered_as_usual(
    front: McpPersonFront, person: Person
) -> None:
    person.outcome = ToolOutcome("debug: head.payload.sigOfAnother", is_error=False)

    reply = post(
        front,
        rpc("tools/call", call_params("read_note")),
        (b"authorization", b"Bearer head.payload.sigOfTheRequest"),
    )

    assert reply.status == 200
    assert "sigOfAnother" in reply.json()["result"]["content"][0]["text"]


def test_an_outcome_that_cannot_be_written_answers_the_failed_result_and_leaks_nothing(
    front: McpPersonFront, person: Person
) -> None:
    person.outcome = ToolOutcome("lone \ud800 surrogate", is_error=False)
    person.failure = None
    unwritable = post(front, rpc("tools/call", call_params("read_note"))).json()["result"]
    person.outcome = ToolOutcome("done", is_error=False)
    person.failure = RuntimeError("x")
    raised = post(front, rpc("tools/call", call_params("read_note"))).json()["result"]

    assert unwritable == raised


@pytest.mark.parametrize("is_error", [False, True])
def test_one_info_record_names_the_tool_and_whether_the_call_failed(
    front: McpPersonFront, person: Person, caplog: pytest.LogCaptureFixture, is_error: bool
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    person.outcome = ToolOutcome("text", is_error=is_error)

    post(front, rpc("tools/call", call_params("read_note")))

    [record] = [r for r in caplog.records if r.levelno == logging.INFO]
    assert ("read_note" in record.getMessage(), str(is_error) in record.getMessage()) == (
        True,
        True,
    )


def test_two_tools_of_one_name_are_refused_at_construction(person: Person) -> None:
    tools = [person.tool("read_note"), person.tool("read_note")]

    with pytest.raises(ValueError, match="more than once"):
        McpPersonFront(tools, resolve_person=person.resolve)


def test_an_exact_starlette_route_serves_initialize_list_and_call_without_a_redirect(
    front: McpPersonFront,
) -> None:
    app = Starlette(routes=[Route(MCP_ENDPOINT_PATH, front)])

    def headers(version: str | None) -> dict[str, str]:
        return {"MCP-Protocol-Version": version} if version else {}

    with TestClient(app, base_url="http://notes.internal", follow_redirects=False) as client:
        initialized = client.post(
            MCP_ENDPOINT_PATH,
            content=rpc("initialize", {"protocolVersion": VERSION, "capabilities": {}}),
            headers=headers(None),
        )
        acknowledged = client.post(
            MCP_ENDPOINT_PATH,
            content=rpc("notifications/initialized", notification=True),
            headers=headers(VERSION),
        )
        listed = client.post(MCP_ENDPOINT_PATH, content=rpc("tools/list"), headers=headers(VERSION))
        called = client.post(
            MCP_ENDPOINT_PATH,
            content=rpc("tools/call", call_params("read_note")),
            headers=headers(VERSION),
        )

    assert [r.status_code for r in (initialized, acknowledged, listed, called)] == [
        200,
        202,
        200,
        200,
    ]
    assert acknowledged.content == b""
    assert json.loads(called.content)["result"]["isError"] is False
