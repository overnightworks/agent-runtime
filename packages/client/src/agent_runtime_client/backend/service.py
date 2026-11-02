"""The agent service as a backend: one POST, its SSE stream read under the client's rules."""

from __future__ import annotations

import http
import re
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Final
from uuid import UUID

import httpx2
from pydantic import ValidationError

from agent_runtime_client.backend import (
    MAX_RETRY_AFTER_S,
    AgentBackend,
    Asker,
    RunRegistrationRefused,
    RunTokens,
    ServiceRunLifecycle,
    TurnEvent,
    TurnStopKind,
    TurnStopped,
)
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.run_stream import RunStream, StreamViolation
from agent_runtime_client.sse import RunStreamDecoder
from agent_runtime_client.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_runtime_client.wire import (
    AssistantText,
    Cancelled,
    Failed,
    FailureCode,
    RejectionCode,
    RunEvent,
    RunRejection,
    RunRequest,
    RunStarted,
    Succeeded,
    ToolCall,
    ToolResult,
)

RUNS_PATH: Final = "/runs"
WORKLOAD_TOKEN_HEADER: Final = "Agent-Workload-Token"  # noqa: S105 - a header name and no secret
CONNECT_TIMEOUT_S: Final = 10.0
OTHER_TIMEOUT_S: Final = 30.0
# A run's heartbeat comes at most every 300 seconds (wire.RunStarted); the read outlasts one.
READ_TIMEOUT_S: Final = 330.0
TRANSPORT_LOST: Final = "transport_lost"
MALFORMED_TOKEN: Final = "malformed_token"  # noqa: S105 - a closed label and no secret
_TIMEOUT: Final = httpx2.Timeout(OTHER_TIMEOUT_S, connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S)
_RETRY_AFTER_HEADER: Final = "Retry-After"


class ServiceBackend(AgentBackend):
    def __init__(self, config: AgentConfig, lifecycle: ServiceRunLifecycle | None = None) -> None:
        self._service_url = str(config.service_url)
        self._lifecycle = lifecycle

    async def run(self, request: RunRequest, asker: Asker) -> AsyncGenerator[TurnEvent, None]:
        tokens = await asker.tokens()
        if not _header_safe(tokens):
            raise TurnStopped(
                TurnStopKind.REFUSED, RejectionCode.UNAUTHORIZED, detail=MALFORMED_TOKEN
            )
        stream = RunStream()
        held: UUID | None = None
        started: UUID | None = None
        try:
            async with (
                httpx2.AsyncClient(base_url=self._service_url, timeout=_TIMEOUT) as client,
                client.stream(
                    "POST",
                    RUNS_PATH,
                    content=request.model_dump_json(),
                    headers=_headers(tokens),
                ) as response,
            ):
                if response.status_code != http.HTTPStatus.OK:
                    raise await _refusal(response)
                async for decoded in _decoded(response):
                    if isinstance(decoded, StreamViolation):
                        raise _cut_short(stream, started, decoded.kind.value)
                    stream.accept(decoded)
                    if isinstance(decoded, RunStarted):
                        started = decoded.run_id
                        held = self._register(started, asker)
                    event = _turn_event(decoded)
                    if event is not None:
                        yield event
                    if isinstance(event, FinalEvent):
                        return
        except httpx2.HTTPError:
            raise _cut_short(stream, started, TRANSPORT_LOST) from None
        finally:
            if held is not None and self._lifecycle is not None:
                self._lifecycle.release(held)

    def _register(self, run_id: UUID, asker: Asker) -> UUID | None:
        if self._lifecycle is None:
            return None
        try:
            self._lifecycle.register(run_id, asker.principal)
        except RunRegistrationRefused:
            raise TurnStopped(TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE) from None
        return run_id


_VISIBLE_ASCII: Final = re.compile(r"[\x21-\x7e]+")


def _header_safe(tokens: RunTokens) -> bool:
    # The HTTP layer would quote a rejected header value in its error and its debug log.
    values = (tokens.workload, tokens.person)
    return all(
        _VISIBLE_ASCII.fullmatch(value.get_secret_value()) for value in values if value is not None
    )


def _headers(tokens: RunTokens) -> dict[str, str]:
    headers = {
        WORKLOAD_TOKEN_HEADER: tokens.workload.get_secret_value(),
        "Content-Type": "application/json",
    }
    if tokens.person is not None:
        headers["Authorization"] = f"Bearer {tokens.person.get_secret_value()}"
    return headers


async def _decoded(response: httpx2.Response) -> AsyncIterator[RunEvent | StreamViolation]:
    decoder = RunStreamDecoder()
    async for chunk in response.aiter_bytes():
        for decoded in decoder.feed(chunk):
            yield decoded
    for violation in decoder.close():
        yield violation


def _turn_event(event: RunEvent) -> TurnEvent | None:
    match event:
        case AssistantText():
            return AssistantTextEvent(text=event.text)
        case ToolCall():
            return ToolCallEvent(tool_use_id=event.tool_use_id, name=event.name, input=event.input)
        case ToolResult():
            return ToolResultEvent(
                tool_use_id=event.tool_use_id, content=event.content, is_error=event.is_error
            )
        case Succeeded():
            return FinalEvent(text=event.text, files=event.files)
        case Failed():
            raise TurnStopped(
                TurnStopKind.FAILED,
                event.code,
                after_side_effect=event.code is FailureCode.INTERRUPTED,
            )
        case Cancelled():
            raise TurnStopped(TurnStopKind.CANCELLED)
    return None


def _cut_short(stream: RunStream, started: UUID | None, detail: str) -> TurnStopped:
    """How a turn ends when its stream breaks: as the stream's own cut-short rule decides."""
    if started is None:
        return TurnStopped(TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE, detail=detail)
    ending = stream.cancellation_terminal()
    if isinstance(ending, Failed):
        return TurnStopped(TurnStopKind.FAILED, ending.code, after_side_effect=True, detail=detail)
    return TurnStopped(TurnStopKind.CANCELLED, detail=detail)


async def _refusal(response: httpx2.Response) -> TurnStopped:
    try:
        code = RunRejection.model_validate_json(await response.aread()).code
    except ValidationError:
        return TurnStopped(TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE)
    if code is not RejectionCode.BUSY:
        return TurnStopped(TurnStopKind.REFUSED, code)
    wait = _wait_seconds(response.headers.get(_RETRY_AFTER_HEADER))
    return TurnStopped(TurnStopKind.REFUSED, code, retry_after_s=wait)


def _wait_seconds(retry_after: str | None) -> int | None:
    if retry_after is None or not retry_after.isascii() or not retry_after.isdigit():
        return None
    return min(int(retry_after), MAX_RETRY_AFTER_S)
