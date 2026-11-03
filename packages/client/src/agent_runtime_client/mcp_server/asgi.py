"""The ASGI plumbing both MCP fronts share: header fields, the request body, the written answer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from http import HTTPStatus
from typing import Any, Final

from agent_runtime_client.mcp_wire import PROTOCOL_VERSION_HEADER, HttpAnswer, ToolDefinition
from agent_runtime_client.tools import ToolDeclaration
from agent_runtime_client.wire import MAX_EVENT_BYTES

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]

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


def definition_of(declaration: ToolDeclaration, *, read_only_hint: bool = False) -> ToolDefinition:
    """The listed tool; ``readOnlyHint`` is carried only when asked for, else absent."""
    members: dict[str, Any] = {
        "name": declaration.name,
        "description": declaration.description,
        "inputSchema": declaration.parameters,
    }
    if read_only_hint:
        members["annotations"] = {"readOnlyHint": True}
    return ToolDefinition.model_validate(members)


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
