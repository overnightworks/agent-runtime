"""The ASGI server an app mounts to serve its tools over the JSON-only MCP profile.

``McpServer.in_front_of(host_app)`` is the outermost ASGI app: it owns ``MCP_ENDPOINT_PATH``
and everything under ``MCP_ENDPOINT_PATH + "/"``, so no router of the host answers there with
a redirect or a middleware. It answers the first failing head check with an empty body, in
this order: Host, Origin, authorization, path, method, body size. It is stateless: each handler
gets exactly the claims of its own request's capability. The capability proves the signed run
id and nothing about the run being live, so the handler's ownership check stays mandatory
(decision record 0001, O4 and H3).

Once a capability is verified, every body written for that request is asked
``carries_signature_of(token, body)``; a hit writes an empty 500 instead. A handler failure
whose traceback carries the signature becomes ``HandlerFailureWithheld``. The log names only
closed labels, never caller-controlled text.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Final

from pydantic import TypeAdapter

from agent_runtime_client.capability import (
    MCP_ENDPOINT_PATH,
    CapabilityClaims,
    McpOrigin,
    carries_signature_of,
)
from agent_runtime_client.errors import raise_without_chain
from agent_runtime_client.mcp_server.asgi import (
    HEADER_ENCODING,
    LOGGER_NAME,
    WITHHELD,
    AsgiApp,
    Receive,
    Scope,
    Send,
    definition_of,
    field_values,
    protocol_version,
    read_body,
    request_label,
    write,
)
from agent_runtime_client.mcp_server.verification import (
    CapabilityRefusal,
    CapabilityVerificationKey,
    verify_capability,
)
from agent_runtime_client.mcp_wire import (
    ACKNOWLEDGEMENT,
    Answered,
    Call,
    HttpAnswer,
    JsonRpcErrorCode,
    Listing,
    Notified,
    RequestId,
    ToolDefinition,
    ToolOutcome,
    Unoffered,
    answer_for,
    decide_server_message,
    error_response,
    tool_result_response,
    tools_list_response,
)
from agent_runtime_client.tools import ToolDeclaration
from agent_runtime_client.wire import JsonObject

type ToolHandler = Callable[[CapabilityClaims, JsonObject], Awaitable[ToolOutcome]]

_LOG: Final = logging.getLogger(LOGGER_NAME)
_HTTPS_SCHEME: Final = "https://"
_BEARER_PREFIX: Final = "Bearer "
_ORIGIN: Final[TypeAdapter[str]] = TypeAdapter(McpOrigin)


class HandlerFailureWithheld(Exception):
    """A handler failed with the capability in its traceback; the failure itself is dropped."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"the failure of tool {tool_name} carried the capability")
        self.tool_name = tool_name


@dataclass(frozen=True)
class McpTool:
    declaration: ToolDeclaration
    handler: ToolHandler


@dataclass(frozen=True)
class _Verified:
    token: str = field(repr=False)
    claims: CapabilityClaims


def _is_under_endpoint(scope: Scope) -> bool:
    if scope["type"] != "http":
        return False
    path = scope["path"]
    return path == MCP_ENDPOINT_PATH or path.startswith(f"{MCP_ENDPOINT_PATH}/")


class McpServer:
    """One app's MCP endpoint: an ASGI 3 app that serves only ``MCP_ENDPOINT_PATH``."""

    def __init__(
        self,
        origin: str,
        key: CapabilityVerificationKey,
        tools: Sequence[McpTool],
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._origin = _ORIGIN.validate_python(origin)
        self._host = self._origin.removeprefix(_HTTPS_SCHEME).encode("ascii")
        self._key = key
        self._wall_clock = wall_clock
        self._tools = {tool.declaration.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("a tool name is declared more than once")
        self._definitions = {
            name: definition_of(tool.declaration) for name, tool in self._tools.items()
        }

    def in_front_of(self, host_app: AsgiApp) -> AsgiApp:
        """The app to serve: the MCP prefix goes to this server, every other scope to the host."""

        async def front(scope: Scope, receive: Receive, send: Send) -> None:
            target = self if _is_under_endpoint(scope) else host_app
            await target(scope, receive, send)

        return front

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise TypeError(f"the MCP server serves http only, not {scope['type']}")
        verified = self._verified_before_anything_else(scope)
        if isinstance(verified, HTTPStatus):
            await write(send, HttpAnswer(verified, None, b""))
            return
        answer, body = await self._answered_after_verification(verified, scope, receive)
        await write(send, self._cleared(verified, answer, body))

    def _verified_before_anything_else(self, scope: Scope) -> _Verified | HTTPStatus:
        if field_values(scope, b"host") != [self._host]:
            return HTTPStatus.FORBIDDEN
        if field_values(scope, b"origin"):
            return HTTPStatus.FORBIDDEN
        return self._verified(scope)

    async def _answered_after_verification(
        self, verified: _Verified, scope: Scope, receive: Receive
    ) -> tuple[HttpAnswer, bytes]:
        """The answer and the request body it answers; every refusal here is checked at the exit."""
        if scope["path"] != MCP_ENDPOINT_PATH or scope["query_string"]:
            return HttpAnswer(HTTPStatus.NOT_FOUND, None, b""), b""
        if scope["method"] != "POST":
            return HttpAnswer(HTTPStatus.METHOD_NOT_ALLOWED, None, b""), b""
        body = await read_body(receive)
        if body is None:
            return HttpAnswer(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, None, b""), b""
        return await self._answer(verified, body, protocol_version(scope)), body

    def _verified(self, scope: Scope) -> _Verified | HTTPStatus:
        authorizations = field_values(scope, b"authorization")
        if len(authorizations) != 1:
            return HTTPStatus.UNAUTHORIZED
        value = authorizations[0].decode(HEADER_ENCODING)
        if not value.startswith(_BEARER_PREFIX):
            return HTTPStatus.UNAUTHORIZED
        token = value.removeprefix(_BEARER_PREFIX)
        verdict = verify_capability(
            token, key=self._key, origin=self._origin, now=self._wall_clock()
        )
        if isinstance(verdict, CapabilityRefusal):
            _LOG.warning("a capability was refused: %s", verdict.value)
            return HTTPStatus.UNAUTHORIZED
        return _Verified(token, verdict)

    def _allowed_definitions(self, claims: CapabilityClaims) -> list[ToolDefinition]:
        allowed = frozenset(claims.tools)
        return [definition for name, definition in self._definitions.items() if name in allowed]

    async def _answer(
        self, verified: _Verified, body: bytes, protocol_version: str | None
    ) -> HttpAnswer:
        allowed = self._allowed_definitions(verified.claims)
        decision = decide_server_message(
            body, protocol_version, frozenset(definition.name for definition in allowed)
        )
        match decision:
            case Answered(answer):
                return answer
            case Notified():
                return ACKNOWLEDGEMENT
            case Listing(request_id):
                return answer_for(tools_list_response(request_id, allowed))
            case Unoffered(request_id):
                return answer_for(error_response(request_id, JsonRpcErrorCode.INVALID_PARAMS))
            case Call(request_id, params):
                return await _called(
                    self._tools[params.name], verified, request_id, params.arguments
                )
            case _:
                raise AssertionError("a server decision is not handled")

    def _cleared(self, verified: _Verified, answer: HttpAnswer, request_body: bytes) -> HttpAnswer:
        if not carries_signature_of(verified.token, answer.body):
            return answer
        _LOG.warning("an answer was withheld: %s", request_label(request_body, self._tools))
        return WITHHELD


async def _called(
    tool: McpTool, verified: _Verified, request_id: RequestId, arguments: JsonObject
) -> HttpAnswer:
    """The tool's answer; a failure of the handler or of writing its outcome is the same failure."""
    outcome_text = ""
    try:
        outcome = await tool.handler(verified.claims, arguments)
        outcome_text = f"{outcome.content}{outcome.is_error}"
        return answer_for(tool_result_response(request_id, outcome))
    except Exception as failure:
        formatted = "".join(traceback.format_exception(failure)) + outcome_text
        if not carries_signature_of(verified.token, formatted.encode("utf-8", errors="replace")):
            raise
        name = tool.declaration.name
        _LOG.warning("a failure was withheld: tool %s", name)
        raise_without_chain(HandlerFailureWithheld(name))
