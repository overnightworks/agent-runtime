"""One chat-completion round read from an event stream: framed, bounded and assembled whole.

The reader takes byte chunks, hands out each text piece once it is checked and then the round
the stream completed. It imports no HTTP library, and its exceptions hold no text from the wire.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Final, Literal, NamedTuple, TypedDict

from pydantic import JsonValue, ValidationError

from agents.turn import MAX_EVENT_BYTES, JsonObject, TurnUsage, loaded_json

# JSON writes one byte of text as up to six (a \u00XX escape); the rest is the envelope.
_JSON_ESCAPE_FACTOR: Final = 6
_ENVELOPE_ALLOWANCE_BYTES: Final = 65_536
_MAX_DATA_BYTES: Final = _JSON_ESCAPE_FACTOR * MAX_EVENT_BYTES + _ENVELOPE_ALLOWANCE_BYTES
_ERROR_TYPE: Final = re.compile(r"[a-z0-9_]{1,64}")
_ERROR_CODE: Final = re.compile(r"\d{3}", re.ASCII)
_DONE: Final = "[DONE]"


class MalformedRound(Exception):
    """The stream is no complete round, or passes a bound."""


class RoundErrorEvent(Exception):
    """The gateway sent an error event; ``kind`` is its checked type and code, never its message."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class AssembledFunction(TypedDict):
    name: str
    arguments: str


class AssembledToolCall(TypedDict):
    """One function call as the round assembled it; the gateway echoes it back unchanged."""

    id: str
    type: Literal["function"]
    function: AssembledFunction


class AssembledMessage(TypedDict):
    """The assistant message a round assembled: its text or ``None``, and its calls, maybe none."""

    role: Literal["assistant"]
    content: str | None
    tool_calls: list[AssembledToolCall]


@dataclass(frozen=True)
class StreamedRound:
    """What a buffered completion carried: the finish reason, the assistant message and the usage.

    ``usage`` is ``None`` when the round sent none or sent one that cannot be read.
    """

    finish_reason: str
    message: AssembledMessage
    usage: TurnUsage | None = None


@dataclass
class _Call:
    id: str
    name: str
    arguments: list[str] = field(default_factory=list[str])


@dataclass
class _Assembly:
    """The round so far; every piece is checked as it is added."""

    round_limit_bytes: int
    text: list[str] = field(default_factory=list[str])
    text_bytes: int = 0
    calls: list[_Call] = field(default_factory=list[_Call])
    held_bytes: int = 0
    finish_reason: str | None = None
    usage: TurnUsage | None = None

    def add_text(self, piece: str) -> None:
        size = _utf8_size(piece)
        if self.text_bytes + size > MAX_EVENT_BYTES:
            raise MalformedRound
        self.text_bytes += size
        self._hold(size)
        self.text.append(piece)

    def add_fragment(self, fragment: JsonValue) -> None:
        if not isinstance(fragment, dict):
            raise MalformedRound
        index = fragment.get("index")
        function = fragment.get("function", {})
        if not isinstance(index, int) or isinstance(index, bool) or not isinstance(function, dict):
            raise MalformedRound
        call_id, name, arguments = (
            fragment.get("id"),
            function.get("name"),
            function.get("arguments"),
        )
        if index == len(self.calls):
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise MalformedRound
            self.calls.append(_Call(call_id, name))
            self._hold(_utf8_size(call_id) + _utf8_size(name))
        elif not 0 <= index < len(self.calls):
            raise MalformedRound
        call = self.calls[index]
        if call_id not in (None, call.id) or name not in (None, call.name):
            raise MalformedRound
        if arguments is not None:
            if not isinstance(arguments, str):
                raise MalformedRound
            self._hold(_utf8_size(arguments))
            call.arguments.append(arguments)

    def _hold(self, size: int) -> None:
        self.held_bytes += size
        if self.held_bytes > self.round_limit_bytes:
            raise MalformedRound

    def finished(self) -> StreamedRound:
        if self.finish_reason is None:
            raise MalformedRound
        calls: list[AssembledToolCall] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": "".join(call.arguments)},
            }
            for call in self.calls
        ]
        message: AssembledMessage = {
            "role": "assistant",
            "content": "".join(self.text) or None,
            "tool_calls": calls,
        }
        return StreamedRound(self.finish_reason, message, self.usage)


class CheckedError(NamedTuple):
    """An error's ``type`` and ``code``, both well formed."""

    error_type: str
    code: str


def checked_error_of(error: JsonValue) -> CheckedError | None:
    """The error's ``type`` and ``code`` when both are well formed; never its other fields."""
    if isinstance(error, dict):
        error_type, code = error.get("type"), error.get("code")
        if (
            isinstance(error_type, str)
            and _ERROR_TYPE.fullmatch(error_type)
            and isinstance(code, str)
            and _ERROR_CODE.fullmatch(code)
        ):
            return CheckedError(error_type, code)
    return None


def error_kind_of(error: JsonValue) -> str:
    """The text a log gives for the error's checked ``type`` and ``code``."""
    checked = checked_error_of(error)
    if checked is None:
        return "no error type given"
    return f"error type {checked.error_type}, code {checked.code}"


async def read_round(
    chunks: AsyncIterable[bytes], *, round_limit_bytes: int
) -> AsyncGenerator[str | StreamedRound, None]:
    """Each non-empty text piece as its event is added, then the round it completes.

    The round is complete at ``[DONE]`` after a finish reason; anything else is a failure.
    """
    assembly = _Assembly(round_limit_bytes)
    async with aclosing(_events(chunks)) as events:
        async for data in events:
            if data == _DONE:
                yield assembly.finished()
                return
            if piece := _take(assembly, data):
                yield piece
    raise MalformedRound


async def _events(chunks: AsyncIterable[bytes]) -> AsyncGenerator[str, None]:
    """The data of each event, framed by the HTML standard's rules for an event stream.

    Only LF ends a line (a trailing CR is stripped): a lone CR as a line end, the standard's one
    further rule, is left out since the gateway writes none. Data pending at the end is dropped.
    """
    data: list[str] = []
    data_bytes = 0
    async with aclosing(_lines(chunks)) as lines:
        async for line in lines:
            if not line:
                if data:
                    yield "\n".join(data)
                data, data_bytes = [], 0
            elif (value := _data_of(line)) is not None:
                data_bytes += _utf8_size(value) + bool(data)
                if data_bytes > _MAX_DATA_BYTES:
                    raise MalformedRound
                data.append(value)


async def _lines(chunks: AsyncIterable[bytes]) -> AsyncGenerator[str, None]:
    """Each complete line decoded, its trailing CR and the stream's one leading BOM dropped."""
    pending = bytearray()
    is_first_line = True
    async for chunk in chunks:
        pending += chunk
        while (end := pending.find(b"\n")) != -1:
            if end > _MAX_DATA_BYTES:
                raise MalformedRound
            line = _decoded(bytes(pending[:end]).removesuffix(b"\r"))
            del pending[: end + 1]
            if is_first_line:
                line, is_first_line = line.removeprefix("\ufeff"), False
            yield line
        if len(pending) > _MAX_DATA_BYTES:
            raise MalformedRound


def _data_of(line: str) -> str | None:
    """The value of a ``data`` field line; ``None`` for a comment or any other field."""
    name, _, value = line.partition(":")
    return value.removeprefix(" ") if name == "data" else None


def _take(assembly: _Assembly, data: str) -> str | None:
    """Add one event's chunk to the round; its text piece, if it carries one."""
    event = _parsed(data)
    if "error" in event:
        raise RoundErrorEvent(error_kind_of(event["error"]))
    usage = event.get("usage")
    if usage is not None:
        assembly.usage = _usage_of(usage)
    choices = event.get("choices")
    if choices is None and usage is not None:
        return None
    if not isinstance(choices, list):
        raise MalformedRound
    return _add_choice(assembly, choices[0]) if choices else None


def _usage_of(usage: JsonValue) -> TurnUsage | None:
    """The turn's words for a chunk's usage; ``None`` when a count is missing or not a count."""
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        return None
    try:
        return TurnUsage.model_validate(
            {
                "input_tokens": usage.get("prompt_tokens"),
                "cached_input_tokens": _first_sent(details.get("cached_tokens")),
                "cache_write_tokens": _first_sent(
                    details.get("cache_write_tokens"), usage.get("cache_creation_input_tokens")
                ),
                "output_tokens": usage.get("completion_tokens"),
                "requests": 1,
            }
        )
    except ValidationError:
        return None


def _first_sent(*counts: JsonValue) -> JsonValue:
    """The first count that was sent (a null is not sent), else zero."""
    return next((count for count in counts if count is not None), 0)


def _parsed(data: str) -> JsonObject:
    try:
        event = loaded_json(data)
    except ValueError, RecursionError:
        event = None
    if not isinstance(event, dict):
        raise MalformedRound
    return event


def _add_choice(assembly: _Assembly, choice: JsonValue) -> str | None:
    if not isinstance(choice, dict):
        raise MalformedRound
    delta = choice.get("delta", {})
    if not isinstance(delta, dict):
        raise MalformedRound
    content, fragments = delta.get("content"), delta.get("tool_calls")
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise MalformedRound
    if content is not None:
        if not isinstance(content, str):
            raise MalformedRound
        assembly.add_text(content)
    if fragments is not None:
        if not isinstance(fragments, list):
            raise MalformedRound
        for fragment in fragments:
            assembly.add_fragment(fragment)
    if finish_reason is not None:
        assembly.finish_reason = finish_reason
    return content


def _decoded(line: bytes) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        pass
    raise MalformedRound


def _utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate is no text
        pass
    raise MalformedRound
