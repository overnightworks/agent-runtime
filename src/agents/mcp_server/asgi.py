"""What the person front's ASGI shell needs: header fields, request body, written answer, the
withheld answer and the closed label a withheld answer is logged under."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Container, MutableMapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final

import anyio.lowlevel
from pydantic import JsonValue

from agents.mcp_wire import PROTOCOL_VERSION_HEADER, carries_signature_of
from agents.turn import MAX_EVENT_BYTES

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass(frozen=True)
class HttpAnswer:
    status: HTTPStatus
    content_type: str | None
    body: bytes


LOGGER_NAME: Final = "agents.mcp_server"
HEADER_ENCODING: Final = "iso-8859-1"
_VERSION_SEPARATOR: Final = ", "
_CONTENT_TYPE_FIELD: Final = b"content-type"
_CONTENT_LENGTH_FIELD: Final = b"content-length"
_HEADERS_OF_STATUS: Final = {
    HTTPStatus.UNAUTHORIZED: [(b"www-authenticate", b"Bearer")],
    HTTPStatus.METHOD_NOT_ALLOWED: [(b"allow", b"POST")],
}


def field_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for field_name, value in scope["headers"] if field_name.lower() == name]


def protocol_version(scope: Scope) -> str | None:
    values = field_values(scope, PROTOCOL_VERSION_HEADER.lower().encode("ascii"))
    return _VERSION_SEPARATOR.join(v.decode(HEADER_ENCODING) for v in values) if values else None


async def read_body(receive: Receive) -> bytes | None:
    """The request body, or ``None`` once it is past ``MAX_EVENT_BYTES``."""
    body = bytearray()
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > MAX_EVENT_BYTES:
            return None
        if message["type"] == "http.disconnect" or not message.get("more_body", False):
            return bytes(body)


async def write(send: Send, answer: HttpAnswer) -> None:
    headers = [(_CONTENT_LENGTH_FIELD, str(len(answer.body)).encode("ascii"))]
    if answer.content_type is not None:
        headers.append((_CONTENT_TYPE_FIELD, answer.content_type.encode("ascii")))
    headers.extend(_HEADERS_OF_STATUS.get(answer.status, []))
    await send({"type": "http.response.start", "status": int(answer.status), "headers": headers})
    await send({"type": "http.response.body", "body": answer.body})


WITHHELD: Final = HttpAnswer(HTTPStatus.INTERNAL_SERVER_ERROR, None, b"")
DISCOVER_METHOD: Final = "server/discover"
LISTEN_METHOD: Final = "subscriptions/listen"
_UNKNOWN_METHOD: Final = "unknown_method"
_UNKNOWN_TOOL: Final = "unknown_tool"
_TOOLS_CALL: Final = "tools/call"
_SERVED_METHODS: Final = frozenset(
    {
        DISCOVER_METHOD,
        "tools/list",
        _TOOLS_CALL,
        "resources/list",
        "resources/read",
        LISTEN_METHOD,
    }
)


class WrittenFrames:
    """What a stream's transport tells its handler: each body it has put on the wire."""

    def __init__(self) -> None:
        self._watcher: Callable[[bytes], None] = lambda frame: None

    def watch(self, watcher: Callable[[bytes], None]) -> None:
        self._watcher = watcher

    def written(self, frame: bytes) -> None:
        self._watcher(frame)


async def write_stream(
    respond: Callable[[Send], Awaitable[None]],
    send: Send,
    bearers: Collection[str],
    frames: WrittenFrames,
    *,
    seconds: float,
) -> bool:
    """Runs ``respond`` for at most ``seconds``, writing every frame that carries no bearer.

    ``frames`` is told each body once it is on the wire, so the handler knows when its notice is
    out rather than handed to the SDK. Answers whether a frame was cut. The response start waits
    for the first body, so a bearer in the first frame still answers ``WITHHELD``; a later one cuts
    the stream with nothing written.
    Each ``send`` is one whole frame, so a bearer never spans two. The SDK's own writes carry no
    timeout, so the limit is what ends a call whose client stopped reading.
    """
    start: Message | None = None
    cut = False

    async def guarded(message: Message) -> None:
        nonlocal start, cut
        if message["type"] == "http.response.start":
            start = message
        elif any(carries_signature_of(token, message.get("body", b"")) for token in bearers):
            cut = True
            if start is not None:
                await write(send, WITHHELD)
            limit.cancel()
            await anyio.lowlevel.checkpoint()
        else:
            if start is not None:
                held, start = start, None
                await send(held)
            await send(message)
            frames.written(message.get("body", b""))

    with anyio.move_on_after(seconds) as limit:
        await respond(guarded)
    return cut


def request_label(document: JsonValue, offered_tools: Container[str]) -> str:
    """Only closed words: a served method and an offered tool name, else a fixed stand-in."""
    if not isinstance(document, dict):
        return _UNKNOWN_METHOD
    method = document.get("method")
    if not isinstance(method, str) or method not in _SERVED_METHODS:
        return _UNKNOWN_METHOD
    if method != _TOOLS_CALL:
        return method
    params = document.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    known = isinstance(name, str) and name in offered_tools
    return f"{method} {name if known else _UNKNOWN_TOOL}"
