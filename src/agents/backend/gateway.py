"""The gateway as a backend: one streamed completion per round of a turn, or one picture.

A turn runs under one deadline. An app can also list the models the gateway offers.

The app's key never leaves this module: it is read per turn or per list, sent as the bearer
and written to no exception or log record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import ssl
from collections.abc import AsyncGenerator, Awaitable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from types import TracebackType
from typing import Final, Literal, NotRequired, Required, TypedDict

import httpx2
from pydantic import JsonValue, TypeAdapter, ValidationError

from agents.agent_file import AgentDefinition, TurnRequest
from agents.backend import (
    MAX_RETRY_AFTER_S,
    AgentBackend,
    TurnEvent,
    TurnPerson,
    TurnStopKind,
    TurnStopped,
)
from agents.backend.streamed_round import (
    AssembledMessage,
    AssembledToolCall,
    MalformedRound,
    RoundErrorEvent,
    StreamedRound,
    checked_error_of,
    error_kind_of,
    read_round,
)
from agents.backend.tool_result import RequestedCall, serve
from agents.config import AgentConfig, ChatGateway
from agents.tools import (
    HandTool,
    JsonObject,
    ToolDeclaration,
    ToolEffect,
)
from agents.turn import (
    MAX_EVENT_BYTES,
    MAX_RUN_FILES_BYTES,
    TOOL_IMAGE_MEDIA_TYPE,
    FailureCode,
    GatewayModelName,
    ModelKind,
    RejectionCode,
    RunFile,
    TurnUsage,
    base64_length,
    loaded_json,
    model_kind_of,
    person_fingerprint,
    written_base64,
)
from agents.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)

_LOGGER: Final = logging.getLogger(__name__)
_COMPLETIONS_PATH: Final = "/v1/chat/completions"
_PICTURES_PATH: Final = "/v1/images/generations"
_MODELS_PATH: Final = "/v1/models"
_BUDGET_EXCEEDED: Final = "budget_exceeded"
_MODEL_LIST_DEADLINE_SECONDS: Final = 10
_PICTURE_FILE_NAME: Final = "picture"
# Their records carry the gateway URL and the response headers; an app gets them only by
# setting these loggers itself.
_TRANSPORT_LOGGERS: Final = ("httpx2", "httpcore2")
_MAX_KEY_BYTES: Final = 1024
_PRINTABLE_ASCII: Final = re.compile(r"[\x21-\x7e]+")
_MAX_REQUEST_BYTES: Final = 4 * MAX_EVENT_BYTES
_MAX_PICTURE_BODY_BYTES: Final = base64_length(MAX_RUN_FILES_BYTES) + MAX_EVENT_BYTES
_UNREADABLE_REPLY: Final = (
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    AttributeError,
    RecursionError,
)
_ARGUMENTS: Final = TypeAdapter(JsonObject)
_MODEL_NAME: Final = TypeAdapter(GatewayModelName)
# The chat format has no error flag on a tool message, so the text carries it to the model.
_ERROR_PREFIX: Final = "Error: "
_NOT_RUN_CONTENT: Final = "not run: one call per reply"


class GatewayDetail(StrEnum):
    """Why a turn or a model list stopped: logged for a turn, public on a list's exception."""

    KEY_UNUSABLE = "key_unusable"
    GATEWAY_REFUSED = "gateway_refused"
    GATEWAY_FAILED = "gateway_failed"
    CONNECTION_FAILED = "connection_failed"
    MALFORMED_REPLY = "malformed_reply"
    TIMED_OUT = "timed_out"


class GatewayBackend(AgentBackend):
    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: Iterable[HandTool] = (),
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        _quiet_transport_loggers()
        self._config = config
        self._transport = transport
        self._tools: dict[str, HandTool] = {}
        for tool in tools:
            if tool.declaration.name in self._tools:
                raise ValueError(f"tool {tool.declaration.name!r} is given twice")
            self._tools[tool.declaration.name] = tool

    async def run(
        self, request: TurnRequest, person: TurnPerson
    ) -> AsyncGenerator[TurnEvent, None]:
        """The turn's events; a stop leaves this frame holding neither the key nor a reply.

        A stop the gateway path raised is replaced by a plain one, with nothing of the frames or
        errors it passed through; a stop the host raised is the same exception, its traceback
        cut of the gateway path's frames, which hold the key, the reply and the call arguments.
        """
        try:
            async with contextlib.aclosing(self._turn(request, person)) as events:
                async for event in events:
                    try:
                        yield event
                    finally:
                        # The next pull or the host's close can stop the turn; this frame then
                        # holds no text piece.
                        del event
        except _GatewayStop as stopped:
            closed = _plain(stopped)
        except TurnStopped as stopped:
            raise stopped.with_traceback(_beyond_the_gateway_path(stopped.__traceback__))
        else:
            return
        raise closed

    async def _turn(
        self, request: TurnRequest, person: TurnPerson
    ) -> AsyncGenerator[TurnEvent, None]:
        tools = self._declared_tools(request)
        if person.role not in request.roles:
            raise _GatewayStop(TurnStopKind.REFUSED, RejectionCode.FORBIDDEN)
        deadline = asyncio.get_running_loop().time() + request.max_duration_seconds
        if request.picture_size is not None:
            yield await _before(
                deadline, self._picture(request, request.picture_size, person, deadline)
            )
            return
        progress = _Progress(request.max_tool_calls or 0)
        try:
            key = await _before(deadline, self._authorise(person))
            offered = frozenset(tools)
            messages = _opening_messages(request)
            user = person_fingerprint(person.principal)
            usages: list[TurnUsage | None] = []
            while True:
                _refuse_after(deadline)
                body = _bounded(_body(request, messages, user))
                opened = self._post(_COMPLETIONS_PATH, body, key)
                async with contextlib.aclosing(_streamed_round(opened, offered, deadline)) as parts:
                    while isinstance(part := await anext(parts), str):
                        yield AssistantTextEvent(text=part)
                        _refuse_after(deadline)
                    reply = part
                usages.append(reply.usage)
                if reply.message is None:
                    yield FinalEvent(text=reply.text, usage=_summed(usages))
                    return
                served = reply.calls[:1] if request.one_call_per_reply else reply.calls
                progress.admit(reply.calls, served)
                messages.append(reply.message)
                for call in served:
                    yield ToolCallEvent(tool_use_id=call.id, name=call.name, input=call.arguments)
                    _refuse_after(deadline)
                    progress.wrote |= tools[call.name].effect is ToolEffect.SIDE_EFFECTING
                    handler = self._tools[call.name].handler
                    result = await _before(deadline, serve(handler, person, call))
                    messages.append(_result_message(result))
                    yield result
                messages += [_result_message(_not_run(call)) for call in reply.calls[len(served) :]]
        except TurnStopped as stopped:
            stopped.after_side_effect = stopped.after_side_effect or progress.wrote
            raise

    def _declared_tools(self, request: TurnRequest) -> dict[str, ToolDeclaration]:
        tools = {declaration.name: declaration for declaration in request.tools}
        for name, declaration in tools.items():
            if name not in self._tools:
                raise ValueError(f"tool {name!r} has no handler")
            if self._tools[name].declaration != declaration:
                raise ValueError(f"tool {name!r} is declared differently than its handler")
        return tools

    async def _authorise(self, person: TurnPerson) -> str:
        if self._config.api_group not in await person.groups():
            raise _GatewayStop(TurnStopKind.REFUSED, RejectionCode.NOT_ENABLED)
        return _read_key(self._config.gateway.key_file)

    async def _picture(
        self, request: TurnRequest, size: str, person: TurnPerson, deadline: float
    ) -> FinalEvent:
        key = await self._authorise(person)
        _refuse_after(deadline)
        body = _bounded(_picture_body(request, size, person_fingerprint(person.principal)))
        async with self._post(_PICTURES_PATH, body, key) as response:
            file = await _picture_of(response)
        return FinalEvent(text="", files=(file,))

    def _post(
        self, path: str, body: str, key: str
    ) -> contextlib.AbstractAsyncContextManager[httpx2.Response]:
        return _opened(self._config.gateway, self._transport, "POST", path, key, content=body)


@dataclass(frozen=True)
class GatewayModels:
    """The model names the gateway offers, by kind; a name of no known kind is not offered."""

    text: tuple[str, ...]
    image: tuple[str, ...]

    def for_agent(self, agent: AgentDefinition) -> tuple[str, ...]:
        """The names this agent's ``request(..., model=...)`` accepts."""
        return self.image if agent.model_kind is ModelKind.IMAGE else self.text


class ModelListUnavailable(Exception):
    """The one way a model list fails; ``detail`` is a closed label, never text from the wire."""

    def __init__(self, detail: GatewayDetail) -> None:
        super().__init__(f"the gateway's model list is unavailable: {detail.value}")
        self.detail = detail


async def list_gateway_models(
    gateway: ChatGateway, *, transport: httpx2.AsyncBaseTransport | None = None
) -> GatewayModels:
    """A bare ``GET /v1/models`` with the app's key, under a fixed deadline."""
    _quiet_transport_loggers()
    try:
        async with asyncio.timeout(_MODEL_LIST_DEADLINE_SECONDS):
            return await _get_models(gateway, transport)
    except TurnStopped as stopped:
        # A 429 and a spent budget carry no detail; for a list each refuses like any other 4xx.
        detail = GatewayDetail(stopped.detail or GatewayDetail.GATEWAY_REFUSED)
    except TimeoutError:
        detail = GatewayDetail.TIMED_OUT
    # Raised outside the handlers, so no underlying exception stays in __context__, and in a
    # frame that never held the key, so its traceback shows none.
    raise ModelListUnavailable(detail)


async def _get_models(
    gateway: ChatGateway, transport: httpx2.AsyncBaseTransport | None
) -> GatewayModels:
    key = _read_key(gateway.key_file)
    async with _opened(gateway, transport, "GET", _MODELS_PATH, key) as response:
        return await _models_of(response)


def _quiet_transport_loggers() -> None:
    for name in _TRANSPORT_LOGGERS:
        transport_logger = logging.getLogger(name)
        if transport_logger.level == logging.NOTSET:
            transport_logger.setLevel(logging.WARNING)


@contextlib.asynccontextmanager
async def _opened(
    gateway: ChatGateway,
    transport: httpx2.AsyncBaseTransport | None,
    method: str,
    path: str,
    key: str,
    *,
    content: str | None = None,
) -> AsyncGenerator[httpx2.Response, None]:
    """The gateway's 200 response, open while it is read; a connection lost meanwhile is raised."""
    headers = {"Authorization": f"Bearer {key}"}
    if content is not None:
        headers["Content-Type"] = "application/json"
    try:
        # ssl.SSLError is an OSError.
        async with (
            _client(gateway, transport) as client,
            client.stream(method, path, content=content, headers=headers) as response,
        ):
            await _raise_unless_ok(response)
            yield response
    except httpx2.HTTPError, httpx2.InvalidURL, OSError:
        raise _unavailable(GatewayDetail.CONNECTION_FAILED) from None


def _client(
    gateway: ChatGateway, transport: httpx2.AsyncBaseTransport | None
) -> httpx2.AsyncClient:
    verify = True
    if gateway.ca_file is not None and transport is None:
        verify = ssl.create_default_context(cafile=str(gateway.ca_file))
    # One client per request keeps cookies from outliving it; the deadline is the only timeout.
    return httpx2.AsyncClient(
        base_url=gateway.origin,
        transport=transport,
        verify=verify,
        trust_env=False,
        follow_redirects=False,
        timeout=None,
    )


class _TextPart(TypedDict):
    type: Literal["text"]
    text: str


class _ImageUrl(TypedDict):
    url: str


class _ImagePart(TypedDict):
    type: Literal["image_url"]
    image_url: _ImageUrl


class _Message(TypedDict):
    role: Required[str]
    content: NotRequired[str | list[_ImagePart | _TextPart] | None]
    tool_call_id: NotRequired[str]
    tool_calls: NotRequired[list[AssembledToolCall]]


class _FunctionSpec(TypedDict):
    name: str
    description: str
    parameters: JsonObject


class _FunctionTool(TypedDict):
    type: str
    function: _FunctionSpec


class _PictureBody(TypedDict):
    model: str
    prompt: str
    size: str
    user: str


class _StreamOptions(TypedDict):
    include_usage: bool


class _Body(TypedDict):
    model: str
    messages: list[_Message]
    user: str
    stream: bool
    stream_options: _StreamOptions
    tools: NotRequired[list[_FunctionTool]]
    parallel_tool_calls: NotRequired[bool]


@dataclass(frozen=True)
class _Round:
    """One judged reply: the answer when the model stopped, else the calls it asked for."""

    text: str
    calls: tuple[RequestedCall, ...]
    message: _Message | None
    usage: TurnUsage | None = None


@dataclass
class _Progress:
    """What a turn has done so far, for the bounds on it and for ``after_side_effect``."""

    max_tool_calls: int
    calls: int = 0
    wrote: bool = False
    call_ids: set[str] = field(default_factory=set[str])

    def admit(self, calls: tuple[RequestedCall, ...], served: tuple[RequestedCall, ...]) -> None:
        ids = {call.id for call in calls}
        if len(ids) != len(calls) or not ids.isdisjoint(self.call_ids):
            raise _malformed_reply()
        if self.calls + len(served) > self.max_tool_calls:
            raise _limit_exceeded()
        self.calls += len(served)
        self.call_ids |= ids


async def _before[Result](deadline: float, step: Awaitable[Result]) -> Result:
    """Await a step of the turn; the turn's one deadline cancels it."""
    scope = asyncio.timeout_at(deadline)
    try:
        async with scope:
            return await step
    except TimeoutError:
        if not scope.expired():
            raise
        raise _limit_exceeded() from None


def _refuse_after(deadline: float) -> None:
    """A step the turn has not started is not started past its deadline."""
    if asyncio.get_running_loop().time() >= deadline:
        raise _limit_exceeded()


def _limit_exceeded() -> TurnStopped:
    return _GatewayStop(TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)


def _opening_messages(request: TurnRequest) -> list[_Message]:
    messages: list[_Message] = [{"role": "system", "content": request.turn.system}]
    messages += [
        _Message(role=message.role.value, content=message.text)
        for message in request.turn.messages
    ]
    return messages


def _not_run(call: RequestedCall) -> ToolResultEvent:
    return ToolResultEvent(tool_use_id=call.id, content=_NOT_RUN_CONTENT, is_error=True)


def _result_message(result: ToolResultEvent) -> _Message:
    text = _ERROR_PREFIX + result.content if result.is_error else result.content
    if result.image is None:
        return {"role": "tool", "tool_call_id": result.tool_use_id, "content": text}
    # a text part the provider would refuse as blank is left out; the image goes first
    url = f"data:{TOOL_IMAGE_MEDIA_TYPE};base64,{written_base64(result.image.content)}"
    parts: list[_ImagePart | _TextPart] = [{"type": "image_url", "image_url": {"url": url}}]
    if text.strip():
        parts.append({"type": "text", "text": text})
    return {"role": "tool", "tool_call_id": result.tool_use_id, "content": parts}


def _body(request: TurnRequest, messages: list[_Message], user: str) -> str:
    body: _Body = {
        "model": request.model,
        "messages": messages,
        "user": user,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        # ``effect`` is for the client, so the projection names its fields.
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
        if request.one_call_per_reply:
            body["parallel_tool_calls"] = False
    return json.dumps(body)


def _summed(usages: list[TurnUsage | None]) -> TurnUsage | None:
    """The turn's usage: its rounds' summed, or ``None`` when any round has none."""
    total = TurnUsage(input_tokens=0, cached_input_tokens=0, cache_write_tokens=0, output_tokens=0)
    for usage in usages:
        if usage is None:
            return None
        total += usage
    return total


def _picture_body(request: TurnRequest, size: str, user: str) -> str:
    prompt = f"{request.turn.system}\n\n{request.turn.messages[0].text}"
    body: _PictureBody = {
        "model": request.model,
        "prompt": prompt,
        "size": size,
        "user": user,
    }
    return json.dumps(body)


def _bounded(body: str) -> str:
    """A request over the one bound is not sent."""
    if len(body.encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise _limit_exceeded()
    return body


def _read_key(key_file: Path) -> str:
    try:
        with key_file.open("rb") as file:
            raw = file.read(_MAX_KEY_BYTES + 1)
    except OSError:
        raise _unavailable(GatewayDetail.KEY_UNUSABLE) from None
    line = raw.decode("ascii", errors="replace").removesuffix("\n")
    if len(raw) > _MAX_KEY_BYTES or _PRINTABLE_ASCII.fullmatch(line) is None:
        raise _unavailable(GatewayDetail.KEY_UNUSABLE)
    return line


class _GatewayStop(TurnStopped):
    """A stop this module's own code raises, with the context and frames of where it arose."""


def _plain(stopped: TurnStopped) -> TurnStopped:
    return TurnStopped(
        stopped.kind,
        stopped.code,
        retry_after_s=stopped.retry_after_s,
        after_side_effect=stopped.after_side_effect,
        detail=stopped.detail,
    )


def _beyond_the_gateway_path(trace: TracebackType | None) -> TracebackType | None:
    """The traceback without the gateway path's frames: they hold the key, the reply and the calls.

    The path is this module and ``tool_result``, whose ``serve`` frame holds the call's arguments.
    """
    path = (globals(), serve.__globals__)
    kept: TracebackType | None = None
    outside: list[TracebackType] = []
    while trace is not None:
        if all(trace.tb_frame.f_globals is not owned for owned in path):
            outside.append(trace)
        trace = trace.tb_next
    for entry in reversed(outside):
        kept = TracebackType(kept, entry.tb_frame, entry.tb_lasti, entry.tb_lineno)
    return kept


def _unavailable(detail: GatewayDetail) -> TurnStopped:
    return _GatewayStop(TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, detail=detail)


async def _raise_unless_ok(response: httpx2.Response) -> None:
    """A 200 passes; any other status ends the turn or the list as the status table says."""
    status = response.status_code
    if status == HTTPStatus.OK:
        return
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        wait = _wait_seconds(response.headers.get("Retry-After"))
        raise _GatewayStop(TurnStopKind.REFUSED, RejectionCode.BUSY, retry_after_s=wait)
    error = await _error_of(response)
    _LOGGER.warning("the gateway answered with status %d, %s", status, error_kind_of(error))
    if status == HTTPStatus.UNPROCESSABLE_CONTENT and _is_spent_budget(error):
        raise _GatewayStop(TurnStopKind.REFUSED, RejectionCode.BUDGET_SPENT)
    is_refusal = HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
    detail = GatewayDetail.GATEWAY_REFUSED if is_refusal else GatewayDetail.GATEWAY_FAILED
    raise _unavailable(detail)


def _is_spent_budget(error: JsonValue) -> bool:
    checked = checked_error_of(error)
    return checked is not None and checked.error_type == _BUDGET_EXCEEDED


async def _error_of(response: httpx2.Response) -> JsonValue:
    """The reply's ``error`` member, or none when the body is unreadable."""
    try:
        document = loaded_json(await _read_body(response, MAX_EVENT_BYTES))
        return document["error"] if isinstance(document, dict) else None
    except (TurnStopped, *_UNREADABLE_REPLY):
        return None


async def _read_body(response: httpx2.Response, limit: int) -> bytes:
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > limit:
                raise _malformed_reply()
    except httpx2.DecodingError:
        raise _malformed_reply() from None
    return bytes(body)


async def _streamed_round(
    opened: contextlib.AbstractAsyncContextManager[httpx2.Response],
    offered: frozenset[str],
    deadline: float,
) -> AsyncGenerator[str | _Round, None]:
    """Each text piece of a round as it streams, then the round, judged whole.

    Opening the request and each read run under the deadline, and no timeout spans a yield. A
    failure is raised after its handler ends.
    """
    failure: TurnStopped | None = None
    completed: list[StreamedRound] = []
    async with contextlib.AsyncExitStack() as stack:
        response = await _before(deadline, stack.enter_async_context(opened))
        chunks = await stack.enter_async_context(contextlib.aclosing(_chunks(response, deadline)))
        reading = read_round(chunks, round_limit_bytes=_MAX_REQUEST_BYTES)
        try:
            async with contextlib.aclosing(reading) as parts:
                while not isinstance(part := await anext(parts), StreamedRound):
                    yield part
                completed.append(part)
        except httpx2.DecodingError, MalformedRound:
            failure = _malformed_reply()
        except RoundErrorEvent as event:
            _LOGGER.warning("the gateway sent an error event, %s", event.kind)
            failure = _unavailable(GatewayDetail.GATEWAY_FAILED)
        if failure is not None:
            raise failure
    yield _judged(completed[0], offered)


async def _chunks(response: httpx2.Response, deadline: float) -> AsyncGenerator[bytes, None]:
    """The response's byte chunks, each read under the turn's deadline."""
    async with contextlib.aclosing(response.aiter_bytes()) as chunks:
        while (chunk := await _before(deadline, anext(chunks, None))) is not None:
            yield chunk


def _judged(streamed: StreamedRound, offered: frozenset[str]) -> _Round:
    try:
        if streamed.finish_reason == "tool_calls":
            return _calls_of(streamed.message, offered, streamed.usage)
        if streamed.finish_reason == "stop":
            return _answer_of(streamed.message["content"], streamed.usage)
    except _UNREADABLE_REPLY:
        pass
    raise _malformed_reply()


async def _models_of(response: httpx2.Response) -> GatewayModels:
    """The ``data`` ids that are gateway model names, by kind; any other id is not offered."""
    body = await _read_body(response, MAX_EVENT_BYTES)
    try:
        document = loaded_json(body)
        entries = document["data"] if isinstance(document, dict) else None
        if not isinstance(entries, list):
            raise _malformed_reply()
        members = [entry["id"] if isinstance(entry, dict) else None for entry in entries]
    except _UNREADABLE_REPLY:
        raise _malformed_reply() from None
    ids = [member for member in members if isinstance(member, str)]
    if len(ids) != len(members):
        raise _malformed_reply()
    names = [model_id for model_id in ids if _is_model_name(model_id)]
    return GatewayModels(
        text=tuple(name for name in names if model_kind_of(name) is ModelKind.TEXT),
        image=tuple(name for name in names if model_kind_of(name) is ModelKind.IMAGE),
    )


def _is_model_name(text: str) -> bool:
    try:
        _MODEL_NAME.validate_python(text, strict=True)
    except ValidationError:
        return False
    return True


async def _picture_of(response: httpx2.Response) -> RunFile:
    """Exactly one entry: a canonical, non-empty ``b64_json`` picture within its bound; no URL."""
    body = await _read_body(response, _MAX_PICTURE_BODY_BYTES)
    try:
        document = loaded_json(body)
        entries = document["data"] if isinstance(document, dict) else None
        if not isinstance(entries, list) or len(entries) != 1:
            raise _malformed_reply()
        entry = entries[0]
        if not isinstance(entry, dict) or entry.get("url") is not None:
            raise _malformed_reply()
        file = RunFile.model_validate(
            {"name": _PICTURE_FILE_NAME, "content": entry["b64_json"]}, strict=True
        )
    except _UNREADABLE_REPLY:
        raise _malformed_reply() from None
    if not file.content or len(file.content) > MAX_RUN_FILES_BYTES:
        raise _malformed_reply()
    return file


def _answer_of(text: str | None, usage: TurnUsage | None) -> _Round:
    if text is None or not text.strip():
        raise _malformed_reply()
    return _Round(text=text, calls=(), message=None, usage=usage)


def _calls_of(
    message: AssembledMessage, offered: frozenset[str], usage: TurnUsage | None
) -> _Round:
    """Every call of a round is judged before any is shown or run."""
    raw_calls = message["tool_calls"]
    if not raw_calls:
        raise _malformed_reply()
    calls: list[RequestedCall] = []
    for raw in raw_calls:
        function = raw["function"]
        name, call_id = function["name"], raw["id"]
        if name not in offered or not call_id:
            raise _malformed_reply()
        arguments = _ARGUMENTS.validate_json(function["arguments"])
        calls.append(RequestedCall(id=call_id, name=name, arguments=arguments))
    reply: _Message = {
        "role": "assistant",
        "content": message["content"],
        "tool_calls": raw_calls,
    }
    return _Round(text="", calls=tuple(calls), message=reply, usage=usage)


def _malformed_reply() -> TurnStopped:
    return _GatewayStop(
        TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR, detail=GatewayDetail.MALFORMED_REPLY
    )


def _wait_seconds(retry_after: str | None) -> int | None:
    """Delay-seconds only: an HTTP-date, a sign or a missing header gives none."""
    if retry_after is None or not retry_after.isascii() or not retry_after.isdigit():
        return None
    digits = retry_after.lstrip("0") or "0"
    if len(digits) > len(str(MAX_RETRY_AFTER_S)):
        return MAX_RETRY_AFTER_S
    return min(int(digits), MAX_RETRY_AFTER_S)
