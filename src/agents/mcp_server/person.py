"""The person front: an app's own MCP endpoint for the agent of one signed-in person.

The host registers ``McpPersonFront`` as the exact route ``MCP_ENDPOINT_PATH``, behind the
middleware that has already admitted or refused the caller, and enters ``running()`` once in its
own lifespan. This front answers no challenge. ``resolve_person`` is the one seam to that
middleware and runs first on every request: its failure, or a result that is not a ``Principal``,
propagates, because a front reached without the gate's admission is a host defect.

A thin ASGI shell stands before the official SDK and serves revision ``2026-07-28`` only: it
refuses every request that is not explicitly that revision, because the SDK's stateless mode
still routes an absent or older version to its legacy path. No answer the front writes, refusals
included, carries the request's bearer. Rights are the route's, so ``tools/list`` offers every
tool. With a ``MemberResources`` port it also serves ``resources/list`` and ``resources/read``
per verified principal, and with ``ResourceChanges`` beside it, ``subscriptions/listen``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from typing import Any, Final

import anyio.lowlevel
from mcp import MCPError
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    INVALID_PARAMS,
    PARSE_ERROR,
    UNSUPPORTED_PROTOCOL_VERSION,
    CallToolRequestParams,
    CallToolResult,
    DiscoverResult,
    ErrorData,
    JSONRPCError,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    RequestParams,
    ResourcesCapability,
    ServerCapabilities,
    SubscriptionsListenRequestParams,
    SubscriptionsListenResult,
    Tool,
    ToolAnnotations,
    ToolsCapability,
    UnsupportedProtocolVersionErrorData,
)
from pydantic import JsonValue, StrictInt, TypeAdapter, ValidationError

from agents.mcp_server.asgi import (
    DISCOVER_METHOD,
    HEADER_ENCODING,
    LISTEN_METHOD,
    LOGGER_NAME,
    WITHHELD,
    HttpAnswer,
    Message,
    Receive,
    Scope,
    Send,
    field_values,
    protocol_version,
    read_body,
    request_label,
    write,
    write_stream,
)
from agents.mcp_server.listen import ResourceChanges, on_subscriptions_listen
from agents.mcp_server.resources import MemberResources, list_answer, read_answer
from agents.mcp_wire import (
    JSON_MEDIA_TYPE,
    PROTOCOL_VERSION,
    carries_signature_of,
    read_strict_json,
)
from agents.member import LISTEN_BOUND_SECONDS
from agents.tools import ToolDeclaration, ToolEffect, ToolOutcome
from agents.turn import (
    TOOL_IMAGE_MEDIA_TYPE,
    JsonObject,
    Principal,
    StrictModel,
    ToolImage,
    UnicodeText,
    is_within_event_bound,
    written_base64,
)

type ResolvePerson = Callable[[Scope], Principal]

_LOG: Final = logging.getLogger(LOGGER_NAME)
_HANDLER_FAILED_TEXT: Final = "the tool failed"
_RESULT_TOO_LARGE_TEXT: Final = "the tool result was too large"
_UNKNOWN_TOOL_TEXT: Final = "Unknown tool"
_INVALID_ARGUMENTS_TEXT: Final = "Invalid tool arguments"
_BEARER_SCHEME: Final = "bearer"
_SERVER_NAME: Final = "overnightworks-agents"
_SERVER_VERSION: Final = "1"
_INITIALIZE: Final = "initialize"
_FINAL_FRAME_SECONDS: Final = 5
_UNSUPPORTED_VERSION_TEXT: Final = "Unsupported protocol version"
_PARSE_ERROR_TEXT: Final = "Parse error"
_SDK_LOGGER_NAMES: Final = ("mcp.server", "mcp.shared")
_ABOVE_ANY_RECORD: Final = logging.CRITICAL + 1
_ECHOABLE_REQUEST_ID: Final[TypeAdapter[int | str]] = TypeAdapter(StrictInt | UnicodeText)
_TOOL_ARGUMENTS: Final[TypeAdapter[JsonObject]] = TypeAdapter(JsonObject)


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


_CURRENT_REQUEST: Final[ContextVar[PersonRequest]] = ContextVar("agents_person_request")


def _listed(declaration: ToolDeclaration) -> Tool:
    """The listed tool; ``readOnlyHint`` is carried only on a read-only one, else absent."""
    read_only = declaration.effect is ToolEffect.READ_ONLY
    return Tool(
        name=declaration.name,
        description=declaration.description,
        input_schema=declaration.parameters,
        annotations=ToolAnnotations(read_only_hint=True) if read_only else None,
    )


class _WritableOutcome(StrictModel):
    """Stricter than the SDK's result model: valid Unicode text and a real boolean."""

    content: UnicodeText
    is_error: bool
    image: ToolImage | None


def _written(outcome: ToolOutcome) -> CallToolResult:
    writable = _WritableOutcome.model_validate(
        {"content": outcome.content, "is_error": outcome.is_error, "image": outcome.image}
    )
    blocks: list[JsonObject] = []
    if writable.image is None or writable.content.strip():
        blocks.append({"type": "text", "text": writable.content})
    if writable.image is not None:
        data = written_base64(writable.image.content)
        blocks.append({"type": "image", "data": data, "mimeType": TOOL_IMAGE_MEDIA_TYPE})
    return CallToolResult.model_validate({"content": blocks, "isError": writable.is_error})


def _refusal(scope: Scope) -> HttpAnswer | None:
    """The head check's refusal, or ``None``: any ``Origin`` and any method but POST are refused."""
    if field_values(scope, b"origin"):
        return HttpAnswer(HTTPStatus.FORBIDDEN, None, b"")
    if scope["method"] != "POST":
        return HttpAnswer(HTTPStatus.METHOD_NOT_ALLOWED, None, b"")
    return None


def _bearers_of(scope: Scope) -> list[str]:
    """Every credential the request presents under the ``bearer`` scheme, in the gate's grammar.

    Reads only; whether the request is admitted is the gate's decision, never this front's.
    """
    credentials: list[str] = []
    for value in field_values(scope, b"authorization"):
        parts = value.decode(HEADER_ENCODING).split()
        if len(parts) == 2 and parts[0].casefold() == _BEARER_SCHEME:
            credentials.append(parts[1])
    return credentials


def _request_id(document: JsonValue) -> int | str | None:
    """The id a refusal can echo; a boolean or a non-Unicode string answers as no id at all."""
    identifier = document.get("id") if isinstance(document, dict) else None
    try:
        return _ECHOABLE_REQUEST_ID.validate_python(identifier)
    except ValidationError:
        return None


def _error_answer(request_id: int | str | None, error: ErrorData) -> HttpAnswer:
    reply = JSONRPCError(jsonrpc="2.0", id=request_id, error=error)
    return HttpAnswer(
        HTTPStatus.BAD_REQUEST, JSON_MEDIA_TYPE, reply.model_dump_json(by_alias=True).encode()
    )


def _revision_refusal(requested: str | None, document: JsonValue) -> HttpAnswer | None:
    """The refusal of a request outside the served revision, or ``None``.

    ``initialize`` belongs to the handshake a modern server never holds, so it is refused even
    at the served version.
    """
    method = document.get("method") if isinstance(document, dict) else None
    if requested == PROTOCOL_VERSION and method != _INITIALIZE:
        return None
    data = UnsupportedProtocolVersionErrorData(
        supported=[PROTOCOL_VERSION], requested=requested or ""
    )
    return _error_answer(
        _request_id(document),
        ErrorData(
            code=UNSUPPORTED_PROTOCOL_VERSION,
            message=_UNSUPPORTED_VERSION_TEXT,
            data=data.model_dump(mode="json"),
        ),
    )


def _replayed(body: bytes, receive: Receive) -> Receive:
    """The body the shell already read, once, then the live channel for anything after it."""
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def replay() -> Message:
        return pending.pop() if pending else await receive()

    return replay


def _answer_of(sent: Sequence[Message]) -> HttpAnswer:
    [start, *bodies] = sent
    content_type = next(
        (value.decode("ascii") for name, value in start["headers"] if name == b"content-type"),
        None,
    )
    body = b"".join(message.get("body", b"") for message in bodies)
    return HttpAnswer(HTTPStatus(start["status"]), content_type, body)


class McpPersonFront:
    """An ASGI 3 app that serves MCP revision 2026-07-28 to one resolved person per request."""

    def __init__(
        self,
        tools: Sequence[PersonTool],
        *,
        resolve_person: ResolvePerson,
        resources: MemberResources | None = None,
        changes: ResourceChanges | None = None,
    ) -> None:
        if changes is not None and resources is None:
            raise ValueError("changes need the resources port they are read from")
        self._tools = {tool.declaration.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("a tool name is declared more than once")
        self._listing = ListToolsResult(
            tools=[_listed(tool.declaration) for tool in self._tools.values()]
        )
        self._resolve_person = resolve_person
        self._server: Server[Any] = Server(
            _SERVER_NAME,
            version=_SERVER_VERSION,
            on_list_tools=self._list_tools,
            on_call_tool=_raising_error_data(self._call_tool),
            on_list_resources=(
                None
                if resources is None
                else _raising_error_data(partial(_list_resources, resources))
            ),
            on_read_resource=(
                None
                if resources is None
                else _raising_error_data(partial(_read_resource, resources))
            ),
            on_subscriptions_listen=(
                None
                if changes is None or resources is None
                else _raising_error_data(partial(_listen, resources, changes))
            ),
        )
        if changes is not None:
            self._server.add_request_handler(DISCOVER_METHOD, RequestParams, _discover_with_listen)
        self._manager: StreamableHTTPSessionManager | None = None

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[None, None]:
        """The SDK's manager for the host's lifespan; a request outside it fails loud.

        The SDK logs request methods and handler failures with their content, so each of its
        server loggers the host left unset is held above any record's level from here on: a
        host's DEBUG on the root logger never reaches it, and a level the host sets on it wins.
        """
        if self._manager is not None:
            raise RuntimeError("the person front is already running")
        for name in _SDK_LOGGER_NAMES:
            sdk_logger = logging.getLogger(name)
            if sdk_logger.level == logging.NOTSET:
                sdk_logger.setLevel(_ABOVE_ANY_RECORD)
        manager = StreamableHTTPSessionManager(
            self._server,
            json_response=True,
            stateless=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        principal = self._resolve_person(scope)
        # A host without a type checker can return anything from its callback.
        if not isinstance(principal, Principal):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"resolve_person returned {type(principal).__name__}, not a Principal")
        manager = self._manager
        if manager is None:
            raise RuntimeError("the person front is not running: enter running() in the lifespan")
        bearers = _bearers_of(scope)
        current = _CURRENT_REQUEST.set(PersonRequest(principal, scope))
        try:
            answer, document = await self._answer(manager, scope, receive, send, bearers)
        finally:
            _CURRENT_REQUEST.reset(current)
        if answer is None:
            return
        if any(carries_signature_of(token, answer.body) for token in bearers):
            _LOG.warning("an answer was withheld: %s", request_label(document, self._tools))
            answer = WITHHELD
        await write(send, answer)

    async def _answer(
        self,
        manager: StreamableHTTPSessionManager,
        scope: Scope,
        receive: Receive,
        send: Send,
        bearers: Sequence[str],
    ) -> tuple[HttpAnswer | None, JsonValue]:
        """The answer, ``None`` once a stream wrote its own, and the request document it answers.

        The document is ``None`` when none was read.
        """
        refusal = _refusal(scope)
        if refusal is not None:
            return refusal, None
        body = await read_body(receive)
        if body is None:
            return HttpAnswer(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, None, b""), None
        try:
            document = read_strict_json(body)
        except ValueError:
            return _error_answer(None, ErrorData(code=PARSE_ERROR, message=_PARSE_ERROR_TEXT)), None
        refused = _revision_refusal(protocol_version(scope), document)
        if refused is not None:
            return refused, document
        label = request_label(document, self._tools)
        if label == LISTEN_METHOD:
            respond = partial(manager.handle_request, scope, _replayed(body, receive))
            cut = await write_stream(
                respond, send, bearers, seconds=LISTEN_BOUND_SECONDS + _FINAL_FRAME_SECONDS
            )
            if cut:
                _LOG.warning("a stream was cut: %s", label)
            return None, document
        sent: list[Message] = []

        async def capture(message: Message) -> None:
            sent.append(message)
            await anyio.lowlevel.checkpoint()

        await manager.handle_request(scope, _replayed(body, receive), capture)
        return _answer_of(sent), document

    async def _list_tools(
        self, _context: ServerRequestContext[Any], _params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        await anyio.lowlevel.checkpoint()
        return self._listing

    async def _call_tool(
        self, _context: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult | ErrorData:
        tool = self._tools.get(params.name)
        if tool is None:
            return ErrorData(code=INVALID_PARAMS, message=_UNKNOWN_TOOL_TEXT)
        try:
            arguments = _TOOL_ARGUMENTS.validate_python(params.arguments or {})
        except ValidationError:
            return ErrorData(code=INVALID_PARAMS, message=_INVALID_ARGUMENTS_TEXT)
        return await self._called(tool, arguments)

    @staticmethod
    async def _called(tool: PersonTool, arguments: JsonObject) -> CallToolResult:
        """The tool's result; every failure answers one fixed text and never raises into the SDK."""
        name = tool.declaration.name
        try:
            outcome = await tool.handler(_CURRENT_REQUEST.get(), arguments)
            if not is_within_event_bound(outcome.content, outcome.image):
                outcome = ToolOutcome(_RESULT_TOO_LARGE_TEXT, is_error=True)
            result = _written(outcome)
        except Exception as failure:
            _LOG.error("a tool failed: %s raised %s", name, type(failure).__name__)
            outcome = ToolOutcome(_HANDLER_FAILED_TEXT, is_error=True)
            result = _written(outcome)
        _LOG.info("a tool was called: %s is_error=%s", name, outcome.is_error)
        return result


def _raising_error_data[Context, Params, Result](
    handler: Callable[[Context, Params], Awaitable[Result | ErrorData]],
) -> Callable[[Context, Params], Awaitable[Result]]:
    """The handler as the SDK types it: the ``ErrorData`` it answers is raised, as the SDK does."""

    async def serve(context: Context, params: Params) -> Result:
        answer = await handler(context, params)
        if isinstance(answer, ErrorData):
            raise MCPError.from_error_data(answer)
        return answer

    return serve


async def _list_resources(
    resources: MemberResources,
    _context: ServerRequestContext[Any],
    _params: PaginatedRequestParams | None,
) -> ListResourcesResult | ErrorData:
    return await list_answer(resources, _CURRENT_REQUEST.get().principal)


async def _read_resource(
    resources: MemberResources,
    _context: ServerRequestContext[Any],
    params: ReadResourceRequestParams,
) -> ReadResourceResult | ErrorData:
    return await read_answer(resources, _CURRENT_REQUEST.get().principal, params.uri)


async def _listen(
    resources: MemberResources,
    changes: ResourceChanges,
    context: ServerRequestContext[Any],
    params: SubscriptionsListenRequestParams,
) -> SubscriptionsListenResult | ErrorData:
    principal = _CURRENT_REQUEST.get().principal
    return await on_subscriptions_listen(resources, changes, principal, context, params)


async def _discover_with_listen(
    _context: ServerRequestContext[Any], _params: RequestParams | None
) -> DiscoverResult:
    """The SDK says every ``listChanged`` is true once listen is served; this front sends none."""
    await anyio.lowlevel.checkpoint()
    return DiscoverResult(
        supported_versions=[PROTOCOL_VERSION],
        capabilities=ServerCapabilities(
            tools=ToolsCapability(list_changed=False),
            resources=ResourcesCapability(subscribe=True, list_changed=False),
        ),
    )
