"""What both MCP fronts share: header fields, request body, written answer, the listed tool,
the withheld answer and the closed label a withheld answer is logged under."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Container, MutableMapping
from http import HTTPStatus
from typing import Any, Final

from agent_runtime_client.mcp_wire import (
    PROTOCOL_VERSION_HEADER,
    AnnotatedToolDefinition,
    HttpAnswer,
    McpMethod,
    McpWireError,
    parse_message,
)
from agent_runtime_client.tools import ToolDeclaration
from agent_runtime_client.turn import MAX_EVENT_BYTES

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]

LOGGER_NAME: Final = "agent_runtime_client.mcp_server"
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


def definition_of(
    declaration: ToolDeclaration, *, read_only_hint: bool = False
) -> AnnotatedToolDefinition:
    """The listed tool; ``readOnlyHint`` is carried only when asked for, else absent."""
    members: dict[str, Any] = {
        "name": declaration.name,
        "description": declaration.description,
        "inputSchema": declaration.parameters,
    }
    if read_only_hint:
        members["annotations"] = {"readOnlyHint": True}
    return AnnotatedToolDefinition.model_validate(members)


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
_UNKNOWN_METHOD: Final = "unknown_method"
_UNKNOWN_TOOL: Final = "unknown_tool"
_KNOWN_METHODS: Final = frozenset(McpMethod)


def request_label(body: bytes, offered_tools: Container[str]) -> str:
    """Only closed words: a profile method and an offered tool name, else a fixed stand-in."""
    try:
        message = parse_message(body)
    except McpWireError:
        return _UNKNOWN_METHOD
    method = getattr(message, "method", None)
    if method not in _KNOWN_METHODS:
        return _UNKNOWN_METHOD
    if method != McpMethod.TOOLS_CALL:
        return method
    name = message.params.get("name")
    known = isinstance(name, str) and name in offered_tools
    return f"{method} {name if known else _UNKNOWN_TOOL}"
