"""The in-process call of one marked route: a single ASGI request into the host's whole application.

``app`` is the object the host's server runs, never a router, so the request passes every
middleware. The inner scope is built from the outer one by an allowlist and carries no cookie,
no ``Origin`` and no outer admission: the host's gate admits the inner request on its own.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Final
from urllib.parse import quote, urlencode

import anyio

from agent_runtime_client.mcp_server.asgi import (
    LOGGER_NAME,
    AsgiApp,
    Message,
    Scope,
    field_values,
)
from agent_runtime_client.tools import JsonObject, ToolOutcome
from agent_runtime_client.turn import MAX_EVENT_BYTES

_LOG: Final = logging.getLogger(LOGGER_NAME)
_ARGUMENTS_REFUSED_TEXT: Final = "the arguments do not fit the tool"
_ROUTE_FAILED_TEXT: Final = "the route did not give a usable answer"
_CARRIED_SCOPE_KEYS: Final = (
    "type",
    "asgi",
    "http_version",
    "scheme",
    "server",
    "client",
    "root_path",
)
_REPLAYED_FIELDS: Final = (b"authorization", b"host", b"x-forwarded-for", b"x-forwarded-proto")
_UNSAFE_SEGMENTS: Final = frozenset({"", ".", ".."})
_PLACEHOLDER: Final = re.compile(r"\{([^{}]+)\}")


def placeholder_names(template: str) -> frozenset[str]:
    """The names a path template holds in braces; the same grammar the call substitutes."""
    return frozenset(_PLACEHOLDER.findall(template))


@dataclass(frozen=True)
class RouteTarget:
    """A marked operation and where each of its arguments goes; ``body`` is ``None`` without one."""

    method: str
    template: str
    path: frozenset[str]
    query: tuple[str, ...]
    body: frozenset[str] | None

    @property
    def names(self) -> frozenset[str]:
        return self.path | frozenset(self.query) | (self.body or frozenset())


@dataclass(frozen=True)
class _InnerRequest:
    path: str
    raw_path: str
    query_string: str
    body: bytes | None


def _query_text(value: object) -> str | None:
    """The query text of a scalar value; ``None`` for one that is no scalar."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        return None
    return json.dumps(value)


def _path_values(target: RouteTarget, arguments: JsonObject) -> dict[str, str] | None:
    values = {}
    for name in target.path:
        value = arguments.get(name)
        if not isinstance(value, str) or value in _UNSAFE_SEGMENTS or "/" in value:
            return None
        values[name] = value
    return values


def _query_pairs(target: RouteTarget, arguments: JsonObject) -> list[tuple[str, str]] | None:
    pairs = []
    for name in target.query:
        if arguments.get(name) is None:
            continue
        text = _query_text(arguments[name])
        if text is None:
            return None
        pairs.append((name, text))
    return pairs


def _inner_request(
    target: RouteTarget, root_path: str, arguments: JsonObject
) -> _InnerRequest | None:
    """The request to make, or ``None`` when the arguments do not fit the tool."""
    values = _path_values(target, arguments)
    pairs = _query_pairs(target, arguments)
    if set(arguments) - target.names or values is None or pairs is None:
        return None
    path = root_path + _PLACEHOLDER.sub(lambda found: values[found[1]], target.template)
    raw_path = root_path + _PLACEHOLDER.sub(
        lambda found: quote(values[found[1]], safe=""), target.template
    )
    body = None
    if target.body is not None:
        body = json.dumps({name: arguments[name] for name in target.body if name in arguments})
    query_string = urlencode(pairs, quote_via=quote)
    return _InnerRequest(path, raw_path, query_string, None if body is None else body.encode())


def _headers_of(outer: Scope, body: bytes | None) -> list[tuple[bytes, bytes]]:
    headers = [(name, value) for name in _REPLAYED_FIELDS for value in field_values(outer, name)]
    headers.append((b"content-length", str(len(body or b"")).encode("ascii")))
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    return headers


def _inner_scope(outer: Scope, target: RouteTarget, request: _InnerRequest) -> Scope:
    scope = {key: outer[key] for key in _CARRIED_SCOPE_KEYS if key in outer}
    scope.update(
        method=target.method,
        path=request.path,
        raw_path=request.raw_path.encode(),
        query_string=request.query_string.encode("ascii"),
        headers=_headers_of(outer, request.body),
    )
    return scope


@dataclass
class _Response:
    """What the app sent, kept within ``MAX_EVENT_BYTES``: no byte past the bound is stored."""

    status: int | None = None
    complete: bool = False
    overflowed: bool = False
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0

    def take(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            return
        chunk = message.get("body", b"")
        if self.size + len(chunk) > MAX_EVENT_BYTES:
            self.overflowed = True
        elif not self.overflowed:
            self.chunks.append(chunk)
            self.size += len(chunk)
        self.complete = not message.get("more_body", False)


def _failure(status: int | None) -> ToolOutcome:
    _LOG.warning("a route gave no usable answer: status=%s", status)
    return ToolOutcome(_ROUTE_FAILED_TEXT, is_error=True)


def _outcome(response: _Response) -> ToolOutcome:
    status = response.status
    if status is None or not response.complete or response.overflowed:
        return _failure(status)
    if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
        is_error = False
    elif HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR:
        is_error = True
    else:
        return _failure(status)
    try:
        return ToolOutcome(b"".join(response.chunks).decode("utf-8"), is_error=is_error)
    except UnicodeDecodeError:
        return _failure(status)


async def call_route(
    app: AsgiApp, outer: Scope, target: RouteTarget, arguments: JsonObject
) -> ToolOutcome:
    """Make the one request; an exception the app raises propagates to the front."""
    request = _inner_request(target, outer.get("root_path", ""), arguments)
    if request is None:
        return ToolOutcome(_ARGUMENTS_REFUSED_TEXT, is_error=True)
    response = _Response()
    finished = anyio.Event()
    body_sent = False

    async def receive() -> Message:
        nonlocal body_sent
        if body_sent:
            await finished.wait()
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": request.body or b"", "more_body": False}

    async def send(message: Message) -> None:
        response.take(message)
        if response.complete:
            finished.set()

    await app(_inner_scope(outer, target, request), receive, send)
    return _outcome(response)
