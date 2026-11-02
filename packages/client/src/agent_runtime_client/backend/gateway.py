"""The gateway as a backend: one buffered chat completion per round of a turn, under one deadline.

The app's key never leaves this module: it is read per turn, sent as the bearer and
written to no exception or log record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from collections.abc import AsyncGenerator, Awaitable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Final, NotRequired, Required, TypedDict

import httpx2
from pydantic import TypeAdapter

from agent_runtime_client.agents import TurnRequest
from agent_runtime_client.backend import (
    MAX_RETRY_AFTER_S,
    AgentBackend,
    TurnEvent,
    TurnPerson,
    TurnStopKind,
    TurnStopped,
)
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.tools import (
    HandTool,
    HandToolHandler,
    JsonObject,
    ToolEffect,
    ToolOutcome,
)
from agent_runtime_client.turn import MAX_EVENT_BYTES, FailureCode, Principal, RejectionCode
from agent_runtime_client.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)

_LOGGER: Final = logging.getLogger(__name__)
_COMPLETIONS_PATH: Final = "/v1/chat/completions"
# A DEBUG record of either library may carry request headers, and the key is one.
_TRANSPORT_LOGGERS: Final = ("httpx2", "httpcore2")
_MAX_KEY_BYTES: Final = 1024
_PRINTABLE_ASCII: Final = re.compile(r"[\x21-\x7e]+")
# JSON writes one byte of text as up to six (a \u00XX escape); the rest is the envelope.
_JSON_ESCAPE_FACTOR: Final = 6
_ENVELOPE_ALLOWANCE_BYTES: Final = 65_536
_MAX_BODY_BYTES: Final = _JSON_ESCAPE_FACTOR * MAX_EVENT_BYTES + _ENVELOPE_ALLOWANCE_BYTES
_MAX_REQUEST_BYTES: Final = 4 * MAX_EVENT_BYTES
_ARGUMENTS: Final = TypeAdapter(JsonObject)
_HANDLER_FAILED_TEXT: Final = "The tool failed."
_RESULT_TOO_LARGE_TEXT: Final = "The tool result was too large to return."
# The chat format has no error flag on a tool message, so the text carries it to the model.
_ERROR_PREFIX: Final = "Error: "


class GatewayDetail(StrEnum):
    """Why a turn stopped, for logs only."""

    KEY_UNUSABLE = "key_unusable"
    GATEWAY_REFUSED = "gateway_refused"
    GATEWAY_FAILED = "gateway_failed"
    CONNECTION_FAILED = "connection_failed"
    MALFORMED_REPLY = "malformed_reply"


class GatewayBackend(AgentBackend):
    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: Iterable[HandTool] = (),
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        for name in _TRANSPORT_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
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
        tools = {declaration.name: declaration for declaration in request.tools}
        for name, declaration in tools.items():
            if name not in self._tools:
                raise ValueError(f"tool {name!r} has no handler")
            if self._tools[name].declaration != declaration:
                raise ValueError(f"tool {name!r} is declared differently than its handler")
        if person.role not in request.roles:
            raise TurnStopped(TurnStopKind.REFUSED, RejectionCode.FORBIDDEN)
        deadline = asyncio.get_running_loop().time() + request.max_duration_seconds
        progress = _Progress(request.max_tool_calls or 0)
        try:
            key = await _before(deadline, self._authorise(person))
            messages = _opening_messages(request)
            user = _fingerprint(person.principal)
            while True:
                _refuse_after(deadline)
                body = _body(request, messages, user)
                if len(body.encode("utf-8")) > _MAX_REQUEST_BYTES:
                    raise _limit_exceeded()
                reply = await _before(deadline, self._post(body, key, frozenset(tools)))
                if reply.message is None:
                    yield AssistantTextEvent(text=reply.text)
                    yield FinalEvent(text=reply.text)
                    return
                progress.admit(reply.calls)
                messages.append(reply.message)
                for call in reply.calls:
                    yield ToolCallEvent(tool_use_id=call.id, name=call.name, input=call.arguments)
                    _refuse_after(deadline)
                    progress.wrote |= tools[call.name].effect is ToolEffect.SIDE_EFFECTING
                    handler = self._tools[call.name].handler
                    result = await _before(deadline, _serve(handler, person, call))
                    yield result
                    messages.append(_result_message(result))
        except TurnStopped as stopped:
            stopped.after_side_effect = stopped.after_side_effect or progress.wrote
            raise

    async def _authorise(self, person: TurnPerson) -> str:
        if self._config.api_group not in await person.groups():
            raise TurnStopped(TurnStopKind.REFUSED, RejectionCode.NOT_ENABLED)
        return _read_key(self._config.gateway.key_file)

    async def _post(self, body: str, key: str, offered: frozenset[str]) -> _Round:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            # ssl.SSLError is an OSError.
            async with (
                self._client() as client,
                client.stream("POST", _COMPLETIONS_PATH, content=body, headers=headers) as response,
            ):
                return await _round_of(response, offered)
        except httpx2.HTTPError, OSError:
            raise _unavailable(GatewayDetail.CONNECTION_FAILED) from None

    def _client(self) -> httpx2.AsyncClient:
        gateway = self._config.gateway
        verify = True
        if gateway.ca_file is not None and self._transport is None:
            verify = ssl.create_default_context(cafile=str(gateway.ca_file))
        # One client per request keeps cookies from outliving it; the deadline is the only timeout.
        return httpx2.AsyncClient(
            base_url=gateway.origin,
            transport=self._transport,
            verify=verify,
            trust_env=False,
            follow_redirects=False,
            timeout=None,
        )


class _ToolCallEcho(TypedDict):
    id: str
    type: str
    function: dict[str, str]


class _Message(TypedDict):
    role: Required[str]
    content: NotRequired[str | None]
    tool_call_id: NotRequired[str]
    tool_calls: NotRequired[list[_ToolCallEcho]]


class _FunctionSpec(TypedDict):
    name: str
    description: str
    parameters: JsonObject


class _FunctionTool(TypedDict):
    type: str
    function: _FunctionSpec


class _Body(TypedDict):
    model: str
    messages: list[_Message]
    user: str
    tools: NotRequired[list[_FunctionTool]]


@dataclass(frozen=True)
class _Call:
    id: str
    name: str
    arguments: JsonObject


@dataclass(frozen=True)
class _Round:
    """One judged reply: the answer when the model stopped, else the calls it asked for."""

    text: str
    calls: tuple[_Call, ...]
    message: _Message | None


@dataclass
class _Progress:
    """What a turn has done so far, for the bounds on it and for ``after_side_effect``."""

    max_tool_calls: int
    calls: int = 0
    wrote: bool = False
    call_ids: set[str] = field(default_factory=set)

    def admit(self, calls: tuple[_Call, ...]) -> None:
        ids = {call.id for call in calls}
        if len(ids) != len(calls) or not ids.isdisjoint(self.call_ids):
            raise _malformed_reply()
        if self.calls + len(calls) > self.max_tool_calls:
            raise _limit_exceeded()
        self.calls += len(calls)
        self.call_ids |= ids


async def _before[Result](deadline: float, step: Awaitable[Result]) -> Result:
    """Await a step of the turn; the turn's one deadline cancels it."""
    try:
        async with asyncio.timeout_at(deadline) as scope:
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
    return TurnStopped(TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)


async def _serve(handler: HandToolHandler, person: TurnPerson, call: _Call) -> ToolResultEvent:
    """Run one handler; an outcome that is no valid result event is a host defect like a raise."""
    is_error: bool | None = None  # unset for a call the deadline or a cancellation cut off
    try:
        event = _result_event(call, await handler(person.principal, call.arguments))
    except Exception as error:  # a handler is the app's code; its failure is a host defect
        _LOGGER.error("tool %s raised %s", call.name, type(error).__qualname__)
        event = _failed_event(call, _HANDLER_FAILED_TEXT)
        is_error = True
        return event
    else:
        is_error = event.is_error
        return event
    finally:
        _LOGGER.info("tool %s call %r finished, is_error=%s", call.name, call.id, is_error)


def _result_event(call: _Call, outcome: ToolOutcome) -> ToolResultEvent:
    event = ToolResultEvent.model_validate(
        {"tool_use_id": call.id, "content": outcome.content, "is_error": outcome.is_error},
        strict=True,
    )
    if len(event.content.encode("utf-8")) > MAX_EVENT_BYTES:
        return _failed_event(call, _RESULT_TOO_LARGE_TEXT)
    return event


def _failed_event(call: _Call, text: str) -> ToolResultEvent:
    return ToolResultEvent(tool_use_id=call.id, content=text, is_error=True)


def _fingerprint(principal: Principal) -> str:
    """The budget identity of a person: one contract with every app, so a change splits budgets."""
    identity = json.dumps([principal.iss, principal.sub], ensure_ascii=False, separators=(",", ":"))
    return "fp-" + sha256(identity.encode("utf-8")).hexdigest()


def _opening_messages(request: TurnRequest) -> list[_Message]:
    messages: list[_Message] = [{"role": "system", "content": request.turn.system}]
    messages += [
        {"role": message.role.value, "content": message.text} for message in request.turn.messages
    ]
    return messages


def _result_message(result: ToolResultEvent) -> _Message:
    content = _ERROR_PREFIX + result.content if result.is_error else result.content
    return {"role": "tool", "tool_call_id": result.tool_use_id, "content": content}


def _body(request: TurnRequest, messages: list[_Message], user: str) -> str:
    body: _Body = {"model": request.model, "messages": messages, "user": user}
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
    return json.dumps(body)


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


def _unavailable(detail: GatewayDetail) -> TurnStopped:
    return TurnStopped(TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, detail=detail)


async def _round_of(response: httpx2.Response, offered: frozenset[str]) -> _Round:
    status = response.status_code
    if status == httpx2.codes.OK:
        return await _judged(response, offered)
    if status == httpx2.codes.TOO_MANY_REQUESTS:
        wait = _wait_seconds(response.headers.get("Retry-After"))
        raise TurnStopped(TurnStopKind.REFUSED, RejectionCode.BUSY, retry_after_s=wait)
    _LOGGER.warning("the gateway answered the turn with status %d", status)
    is_refusal = httpx2.codes.BAD_REQUEST <= status < httpx2.codes.INTERNAL_SERVER_ERROR
    detail = GatewayDetail.GATEWAY_REFUSED if is_refusal else GatewayDetail.GATEWAY_FAILED
    raise _unavailable(detail)


async def _judged(response: httpx2.Response, offered: frozenset[str]) -> _Round:
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > _MAX_BODY_BYTES:
                raise _malformed_reply()
    except httpx2.DecodingError:
        raise _malformed_reply() from None
    try:
        choice = json.loads(body)["choices"][0]
        finish_reason, message = choice["finish_reason"], choice["message"]
        if finish_reason == "tool_calls":
            return _calls_of(message, offered)
        if finish_reason == "stop":
            return _answer_of(message["content"])
    except ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError:
        pass
    raise _malformed_reply()


def _answer_of(text: str) -> _Round:
    if not text.strip() or len(text.encode("utf-8")) > MAX_EVENT_BYTES:
        raise _malformed_reply()
    return _Round(text=text, calls=(), message=None)


def _calls_of(message: JsonObject, offered: frozenset[str]) -> _Round:
    """Every call of a round is judged before any is shown or run."""
    content = message.get("content")
    raw_calls = message["tool_calls"]
    if (content is not None and not isinstance(content, str)) or not raw_calls:
        raise _malformed_reply()
    if content is not None:
        content.encode("utf-8")  # a lone surrogate is a ValueError, judged as a malformed reply
    calls = []
    for raw in raw_calls:
        function = raw["function"]
        name, call_id = function["name"], raw["id"]
        if name not in offered or not isinstance(call_id, str) or not call_id:
            raise _malformed_reply()
        call_id.encode("utf-8")  # as above
        arguments = _ARGUMENTS.validate_json(function["arguments"])
        calls.append(_Call(id=call_id, name=name, arguments=arguments))
    echoed = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": raw["function"]["arguments"]},
        }
        for call, raw in zip(calls, raw_calls, strict=True)
    ]
    reply: _Message = {"role": "assistant", "content": content, "tool_calls": echoed}
    return _Round(text="", calls=tuple(calls), message=reply)


def _malformed_reply() -> TurnStopped:
    return TurnStopped(
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
