"""The person front: an app's own MCP endpoint for the agent of one signed-in person.

The host registers ``McpPersonFront`` as the exact route ``MCP_ENDPOINT_PATH``, behind the
middleware that has already admitted or refused the caller, so this front answers no
challenge. ``resolve_person`` is the one seam to that middleware and runs first on every
request: its failure propagates, because a front reached without the gate's admission is a
host defect. Rights are the route's, so ``tools/list`` offers every tool.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

from agent_runtime_client.mcp_server.asgi import (
    LOGGER_NAME,
    Receive,
    Scope,
    Send,
    definition_of,
    field_values,
    protocol_version,
    read_body,
    write,
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
    Unoffered,
    answer_for,
    decide_server_message,
    error_response,
    tool_result_response,
    tools_list_response,
)
from agent_runtime_client.tools import ToolDeclaration, ToolEffect, ToolOutcome
from agent_runtime_client.turn import Principal
from agent_runtime_client.wire import JsonObject

type ResolvePerson = Callable[[Scope], Principal]

_LOG: Final = logging.getLogger(LOGGER_NAME)
_HANDLER_FAILED_TEXT: Final = "the tool failed"


@dataclass(frozen=True)
class PersonRequest:
    """What a handler knows of its request: the person, and the scope to replay the call from."""

    principal: Principal
    scope: Scope


type PersonToolHandler = Callable[[PersonRequest, JsonObject], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class PersonTool:
    declaration: ToolDeclaration
    handler: PersonToolHandler


def _refusal(scope: Scope) -> HttpAnswer | None:
    """The head check's refusal, or ``None``: any ``Origin`` and any method but POST are refused."""
    if field_values(scope, b"origin"):
        return HttpAnswer(HTTPStatus.FORBIDDEN, None, b"")
    if scope["method"] != "POST":
        return HttpAnswer(HTTPStatus.METHOD_NOT_ALLOWED, None, b"")
    return None


class McpPersonFront:
    """An ASGI 3 app that serves the JSON-only MCP profile to one resolved person per request."""

    def __init__(self, tools: Sequence[PersonTool], *, resolve_person: ResolvePerson) -> None:
        self._tools = {tool.declaration.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("a tool name is declared more than once")
        self._definitions = [
            definition_of(
                tool.declaration, read_only_hint=tool.declaration.effect is ToolEffect.READ_ONLY
            )
            for tool in self._tools.values()
        ]
        self._resolve_person = resolve_person

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = PersonRequest(self._resolve_person(scope), scope)
        await write(send, await self._answer(request, receive))

    async def _answer(self, request: PersonRequest, receive: Receive) -> HttpAnswer:
        refusal = _refusal(request.scope)
        if refusal is not None:
            return refusal
        body = await read_body(receive)
        if body is None:
            return HttpAnswer(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, None, b"")
        return await self._decided(request, body)

    async def _decided(self, request: PersonRequest, body: bytes) -> HttpAnswer:
        decision = decide_server_message(body, protocol_version(request.scope), self._tools)
        match decision:
            case Answered(answer):
                return answer
            case Notified():
                return ACKNOWLEDGEMENT
            case Listing(request_id):
                return answer_for(tools_list_response(request_id, self._definitions))
            case Unoffered(request_id):
                return answer_for(error_response(request_id, JsonRpcErrorCode.INVALID_PARAMS))
            case Call(request_id, params):
                return await self._called(
                    self._tools[params.name], request, request_id, params.arguments
                )
            case _:
                raise AssertionError("a server decision is not handled")

    @staticmethod
    async def _called(
        tool: PersonTool, request: PersonRequest, request_id: RequestId, arguments: JsonObject
    ) -> HttpAnswer:
        name = tool.declaration.name
        try:
            outcome = await tool.handler(request, arguments)
            response = tool_result_response(request_id, outcome)
        except Exception as failure:
            _LOG.error("a tool failed: %s raised %s", name, type(failure).__name__)
            outcome = ToolOutcome(_HANDLER_FAILED_TEXT, is_error=True)
            response = tool_result_response(request_id, outcome)
        _LOG.info("a tool was called: %s is_error=%s", name, outcome.is_error)
        return answer_for(response)
