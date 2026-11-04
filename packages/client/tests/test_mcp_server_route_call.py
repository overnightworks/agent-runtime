"""The in-process call: one ASGI request into the host's whole application per tool call."""

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel

from agent_runtime_client.mcp_server.asgi import AsgiApp
from agent_runtime_client.mcp_server.route_call import RouteTarget, call_route
from agent_runtime_client.tools import ToolOutcome
from agent_runtime_client.wire import MAX_EVENT_BYTES


class Note(BaseModel):
    title: str
    count: int


def outer_scope(root_path: str = "") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "https",
        "server": ("notes.example", 443),
        "client": ("203.0.113.9", 4000),
        "root_path": root_path,
        "method": "POST",
        "path": "/mcp",
        "query_string": b"",
        "headers": [
            (b"host", b"notes.example"),
            (b"authorization", b"Bearer abc"),
            (b"cookie", b"session=secret"),
            (b"origin", b"https://notes.example"),
            (b"accept-encoding", b"gzip"),
            (b"x-forwarded-for", b"203.0.113.9"),
            (b"x-forwarded-for", b"198.51.100.2"),
            (b"x-forwarded-proto", b"https"),
            (b"user-agent", b"agent"),
        ],
        "state": {"admitted": True},
        "extensions": {"admitted": True},
        "user": "admitted",
        "app": object(),
    }


@pytest.fixture
def echo_target() -> RouteTarget:
    return RouteTarget("GET", "/echo/{value}", path=frozenset({"value"}), query=(), body=None)


@pytest.fixture
def search_target() -> RouteTarget:
    return RouteTarget("GET", "/search", path=frozenset(), query=("q", "limit"), body=None)


@pytest.fixture
def notes_target() -> RouteTarget:
    return RouteTarget(
        "POST", "/notes", path=frozenset(), query=(), body=frozenset({"title", "count"})
    )


@pytest.fixture
def echo_app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo/{value}")
    async def echo(value: str, request: Request) -> dict[str, Any]:
        scope = request.scope
        return {
            "value": value,
            "path": scope["path"],
            "raw_path": scope["raw_path"].decode(),
            "headers": [[name.decode(), val.decode()] for name, val in scope["headers"]],
        }

    @app.get("/search")
    async def search(request: Request, limit: int | None = None, q: str | None = None) -> Any:
        return {"query_string": request.scope["query_string"].decode(), "q": q, "limit": limit}

    @app.post("/notes")
    async def create(note: Note, request: Request) -> dict[str, Any]:
        return {
            "title": note.title,
            "count": note.count,
            "headers": dict(request.headers),
            "body_length": len(await request.body()),
        }

    return app


def run(app: AsgiApp, target: RouteTarget, arguments: dict[str, Any], **scope: Any) -> ToolOutcome:
    return asyncio.run(call_route(app, outer_scope(**scope), target, arguments))


@pytest.mark.parametrize(
    ("value", "path", "raw_path"),
    [
        ("a%2Fb", "/echo/a%2Fb", "/echo/a%252Fb"),
        ("a b", "/echo/a b", "/echo/a%20b"),
        ("Ünï", "/echo/Ünï", "/echo/%C3%9Cn%C3%AF"),
        ("100%", "/echo/100%", "/echo/100%25"),
        ("what?", "/echo/what?", "/echo/what%3F"),
        ("a#b", "/echo/a#b", "/echo/a%23b"),
    ],
)
def test_path_values_keep_the_decoded_path_apart_from_the_encoded_raw_path(
    echo_target: RouteTarget, echo_app: FastAPI, value: str, path: str, raw_path: str
) -> None:
    outcome = run(echo_app, echo_target, {"value": value})

    reached = json.loads(outcome.content)
    assert (reached["value"], reached["path"], reached["raw_path"]) == (value, path, raw_path)


def test_the_outer_root_path_prefixes_the_inner_path(
    echo_target: RouteTarget, echo_app: FastAPI
) -> None:
    outcome = run(echo_app, echo_target, {"value": "x"}, root_path="/notes")

    reached = json.loads(outcome.content)
    assert (reached["path"], reached["raw_path"]) == ("/notes/echo/x", "/notes/echo/x")


class RecordingApp:
    def __init__(self, inner: AsgiApp) -> None:
        self.inner = inner
        self.calls = 0
        self.scopes: list[set[str]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        self.scopes.append(set(scope))
        await self.inner(scope, receive, send)


@pytest.mark.parametrize(
    "arguments",
    [
        {"value": ""},
        {"value": "."},
        {"value": ".."},
        {"value": "a/b"},
        {"value": "/"},
        {"value": 7},
        {"value": None},
        {},
        {"value": "ok", "unnamed": "x"},
    ],
)
def test_a_bad_path_value_or_an_unnamed_argument_is_refused_with_no_inner_request(
    echo_target: RouteTarget, echo_app: FastAPI, arguments: dict[str, Any]
) -> None:
    app = RecordingApp(echo_app)

    outcome = run(app, echo_target, arguments)

    assert outcome.is_error
    assert app.calls == 0


@pytest.mark.parametrize("value", [{"a": 1}, ["a"]])
def test_an_object_or_array_query_value_is_refused_with_no_inner_request(
    search_target: RouteTarget, echo_app: FastAPI, value: Any
) -> None:
    app = RecordingApp(echo_app)

    outcome = run(app, search_target, {"q": value})

    assert outcome.is_error
    assert app.calls == 0


@pytest.mark.parametrize(
    ("arguments", "query_string"),
    [
        ({}, ""),
        ({"q": None}, ""),
        ({"q": ""}, "q="),
        ({"q": "a b&c"}, "q=a%20b%26c"),
        ({"limit": 3, "q": "x"}, "q=x&limit=3"),
    ],
)
def test_query_values_reach_the_route_as_sent(
    search_target: RouteTarget, echo_app: FastAPI, arguments: dict[str, Any], query_string: str
) -> None:
    outcome = run(echo_app, search_target, arguments)

    assert not outcome.is_error
    assert json.loads(outcome.content)["query_string"] == query_string


def test_a_boolean_query_value_is_sent_as_its_json_text(echo_app: FastAPI) -> None:
    flagged = RouteTarget("GET", "/search", path=frozenset(), query=("q",), body=None)

    outcome = run(echo_app, flagged, {"q": True})

    assert json.loads(outcome.content)["q"] == "true"


def test_the_routes_own_422_for_an_empty_query_value_is_a_failed_result_with_its_body(
    search_target: RouteTarget,
    echo_app: FastAPI,
) -> None:
    outcome = run(echo_app, search_target, {"limit": ""})

    assert outcome.is_error
    assert "limit" in outcome.content


def test_the_inner_scope_carries_exactly_the_allowlisted_keys(
    echo_target: RouteTarget, echo_app: FastAPI
) -> None:
    app = RecordingApp(echo_app)

    run(app, echo_target, {"value": "x"})

    carried = {"type", "asgi", "http_version", "scheme", "server", "client", "root_path"}
    own = {"method", "path", "raw_path", "query_string", "headers"}
    assert app.scopes == [carried | own]


def test_the_inner_request_replays_exactly_the_named_headers(
    echo_target: RouteTarget, echo_app: FastAPI
) -> None:
    outcome = run(echo_app, echo_target, {"value": "x"})

    assert json.loads(outcome.content)["headers"] == [
        ["authorization", "Bearer abc"],
        ["host", "notes.example"],
        ["x-forwarded-for", "203.0.113.9"],
        ["x-forwarded-for", "198.51.100.2"],
        ["x-forwarded-proto", "https"],
        ["content-length", "0"],
    ]


def test_a_post_sends_its_body_fields_as_one_json_object_with_the_body_headers(
    notes_target: RouteTarget,
    echo_app: FastAPI,
) -> None:
    outcome = run(echo_app, notes_target, {"title": "Dawn", "count": 2})

    reached = json.loads(outcome.content)
    assert (reached["title"], reached["count"]) == ("Dawn", 2)
    assert reached["headers"]["content-type"] == "application/json"
    assert reached["headers"]["content-length"] == str(reached["body_length"])


async def respond(
    send: Any, status: int, *chunks: bytes, complete: bool = True, started: bool = True
) -> None:
    if started:
        await send({"type": "http.response.start", "status": status, "headers": []})
    for number, chunk in enumerate(chunks):
        last = number == len(chunks) - 1
        await send(
            {"type": "http.response.body", "body": chunk, "more_body": not (last and complete)}
        )


def replying(status: int, *chunks: bytes, complete: bool = True, started: bool = True) -> AsgiApp:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        await respond(send, status, *chunks, complete=complete, started=started)

    return app


@pytest.mark.parametrize(
    ("app", "is_error", "content"),
    [
        (replying(200, b"fine"), False, "fine"),
        (replying(201, "ü".encode(), b"!"), False, "ü!"),
        (replying(404, b"no such note"), True, "no such note"),
        (replying(422, b'{"detail":"bad"}'), True, '{"detail":"bad"}'),
    ],
)
def test_a_2xx_gives_the_body_and_a_4xx_gives_a_failed_result_with_the_routes_body(
    echo_target: RouteTarget, app: AsgiApp, is_error: bool, content: str
) -> None:
    outcome = run(app, echo_target, {"value": "x"})

    assert outcome == ToolOutcome(content, is_error=is_error)


@pytest.mark.parametrize(
    "app",
    [
        replying(302, b"moved"),
        replying(307, b""),
        replying(500, b"stack trace with secret"),
        replying(200, b"x" * (MAX_EVENT_BYTES + 1)),
        replying(200, b"x" * MAX_EVENT_BYTES, b"y"),
        replying(404, b"x" * (MAX_EVENT_BYTES + 1)),
        replying(200, b"\xff\xfe"),
        replying(200, b"partial", complete=False),
        replying(200, started=False),
        replying(200),
    ],
    ids=[
        "redirect",
        "slash redirect",
        "server error",
        "body over the bound",
        "chunks over the bound",
        "client error over the bound",
        "not utf-8",
        "incomplete response",
        "no response",
        "headers only",
    ],
)
def test_an_unusable_response_gives_one_failed_result_that_repeats_nothing_of_the_body(
    echo_target: RouteTarget,
    app: AsgiApp,
) -> None:
    outcome = run(app, echo_target, {"value": "x"})

    assert outcome.is_error
    assert outcome == run(replying(500, b"other"), echo_target, {"value": "x"})


def test_a_body_exactly_at_the_bound_is_given(
    echo_target: RouteTarget,
) -> None:
    outcome = run(replying(200, b"x" * MAX_EVENT_BYTES), echo_target, {"value": "x"})

    assert outcome == ToolOutcome("x" * MAX_EVENT_BYTES, is_error=False)


def test_an_exception_the_app_raises_propagates(
    echo_target: RouteTarget,
) -> None:
    async def failing(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run(failing, echo_target, {"value": "x"})


def test_receive_gives_the_body_once_then_blocks_until_the_response_is_complete(
    notes_target: RouteTarget,
) -> None:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["first"] = await receive()
        waiting = asyncio.ensure_future(receive())
        for _ in range(5):
            await asyncio.sleep(0)
        seen["blocked_before_response"] = not waiting.done()
        await respond(send, 200, b"done")
        seen["second"] = await waiting

    run(app, notes_target, {"title": "Dawn", "count": 2})

    assert seen["first"]["type"] == "http.request"
    assert json.loads(seen["first"]["body"]) == {"title": "Dawn", "count": 2}
    assert seen["first"]["more_body"] is False
    assert seen["blocked_before_response"]
    assert seen["second"] == {"type": "http.disconnect"}
