"""The SSE framing of a run's events, owned by ``agent_runtime_client.sse``.

These tests pin the A0b sub-ruling of decision record 0001: one frame per
event, decoded the same however the bytes arrive, and the first frame or
event off the contract named once, with nothing decoded after it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.wire_builders import call, heartbeat, result, started, succeeded, text

from agent_runtime_client import sse as sse_module
from agent_runtime_client.run_stream import StreamViolation, ViolationKind
from agent_runtime_client.sse import (
    Decoded,
    DecoderClosed,
    EventTooLarge,
    RunStreamDecoder,
    encode_event,
)
from agent_runtime_client.wire import (
    MAX_EVENT_BYTES,
    MAX_RUN_FILES_BYTES,
    MAX_SUCCEEDED_FRAME_BYTES,
    MAX_TOOL_INPUT_DEPTH,
    PROTOCOL_VERSION,
    AssistantText,
    Heartbeat,
    RejectionCode,
    RunEvent,
    RunFile,
    RunRejection,
    Succeeded,
)

A_RUN: list[RunEvent] = [
    started(),
    text(1, "Ein erster Absatz – über Grün."),
    call(2, "call-1", name="write_text", tool_input={"line": 1}),
    result(3, "call-1", content="saved\nline two"),
    heartbeat(4),
    succeeded(5),
]
ENCODED = b"".join(encode_event(event) for event in A_RUN)
OPENING = encode_event(A_RUN[0])
HEARTBEAT = encode_event(heartbeat(1))


def _tool_call_frame_with(constant: str) -> bytes:
    frame = encode_event(call(1, "call-1", tool_input={"x": 0}))
    return frame.replace(b'"x":0', b'"x":' + constant.encode())


def _decode(*chunks: bytes) -> list[Decoded]:
    decoder = RunStreamDecoder()
    decoded = [item for chunk in chunks for item in decoder.feed(chunk)]
    return decoded + decoder.close()


SPELLINGS = {
    "LF as encoded": ENCODED,
    "CRLF": ENCODED.replace(b"\n", b"\r\n"),
    "CR": ENCODED.replace(b"\n", b"\r"),
    "byte order mark": b"\xef\xbb\xbf" + ENCODED,
    "comments": b": opened\n\n" + ENCODED.replace(b"\n\n", b"\n: keep-alive\n\n"),
    "retry": b"retry: 3000\n\n" + ENCODED.replace(b"event:", b"retry: 1000\nevent:"),
}

UMLAUT_TEXT_FRAME = (
    b"id: 1\n"
    b"event: assistant_text\n"
    b'data: {"v":%d,"run_id":"8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01","seq":1,'
    b'"type":"assistant_text","text":"Ein erster Absatz \xe2\x80\x93 \xc3\xbcber Gr\xc3\xbcn."}\n'
    b"\n"
) % PROTOCOL_VERSION


def test_an_event_travels_as_one_frame_of_compact_utf8_json() -> None:
    assert encode_event(A_RUN[1]) == UMLAUT_TEXT_FRAME
    assert _decode(OPENING, UMLAUT_TEXT_FRAME)[1] == A_RUN[1]


class _HeartbeatWithAnExtraField(Heartbeat):
    injected: str = "off the v1 contract"


@pytest.mark.parametrize(
    "not_an_event",
    [
        pytest.param(None, id="None"),
        pytest.param(
            RunRejection(v=PROTOCOL_VERSION, code=RejectionCode.BUSY, message="Busy."),
            id="a rejection",
        ),
        pytest.param(
            _HeartbeatWithAnExtraField(v=PROTOCOL_VERSION, run_id=A_RUN[0].run_id, seq=1),
            id="a subclass of an event",
        ),
    ],
)
def test_encoding_something_other_than_a_run_event_is_a_type_error(not_an_event: object) -> None:
    with pytest.raises(TypeError):
        encode_event(not_an_event)


@pytest.mark.parametrize("spelled", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_a_run_decodes_the_same_wherever_its_bytes_split(spelled: bytes) -> None:
    for offset in range(len(spelled) + 1):
        assert _decode(spelled[:offset], spelled[offset:]) == A_RUN, offset


@pytest.mark.parametrize(
    ("frame", "kind"),
    [
        pytest.param(
            HEARTBEAT.replace(b"event:", b"name: beat\nevent:"),
            ViolationKind.MALFORMED_FRAME,
            id="unknown field",
        ),
        pytest.param(
            HEARTBEAT.replace(b"\n\n", b"\ndata: {}\n\n"),
            ViolationKind.MALFORMED_FRAME,
            id="second data line",
        ),
        pytest.param(HEARTBEAT.replace(b"id: 1\n", b""), ViolationKind.MALFORMED_FRAME, id="no id"),
        pytest.param(
            b"id: 1\nevent: heartbeat\ndata: \xff\n\n",
            ViolationKind.MALFORMED_FRAME,
            id="line not UTF-8",
        ),
        pytest.param(
            b"data: " + b"x" * MAX_EVENT_BYTES + b"\n\n",
            ViolationKind.OVERSIZED_FRAME,
            id="frame over the limit",
        ),
        pytest.param(
            HEARTBEAT.replace(b"data: {", b"data: {{"), ViolationKind.INVALID_JSON, id="not JSON"
        ),
        *(
            pytest.param(
                _tool_call_frame_with(constant),
                ViolationKind.INVALID_JSON,
                id=f"{constant} inside a well-formed tool call",
            )
            for constant in ("NaN", "Infinity", "-Infinity")
        ),
        pytest.param(
            b'id: 1\nevent: heartbeat\ndata: ["heartbeat"]\n\n',
            ViolationKind.INVALID_JSON,
            id="JSON array instead of an object",
        ),
        pytest.param(
            HEARTBEAT.replace(b"event: heartbeat", b"event: cancelled"),
            ViolationKind.EVENT_TYPE_MISMATCH,
            id="event field differs from type",
        ),
        pytest.param(
            HEARTBEAT.replace(b"id: 1", b"id: 7"),
            ViolationKind.ID_MISMATCH,
            id="id differs from seq",
        ),
        pytest.param(
            HEARTBEAT.replace(b'"v":%d' % PROTOCOL_VERSION, b'"v":2'),
            ViolationKind.UNSUPPORTED_VERSION,
            id="event off the wire contract",
        ),
        pytest.param(
            encode_event(heartbeat(2)),
            ViolationKind.SEQUENCE_GAP,
            id="event breaking a stream rule",
        ),
    ],
)
def test_a_broken_frame_is_named_and_nothing_is_decoded_after_it(
    frame: bytes, kind: ViolationKind
) -> None:
    decoder = RunStreamDecoder()

    opening, violation = decoder.feed(OPENING + frame + HEARTBEAT)

    assert opening == A_RUN[0]
    assert isinstance(violation, StreamViolation)
    assert violation.kind is kind
    with pytest.raises(DecoderClosed):
        decoder.feed(HEARTBEAT)
    assert decoder.close() == []
    assert decoder.close() == []


def test_feeding_text_instead_of_bytes_is_a_type_error_naming_feed() -> None:
    decoder = RunStreamDecoder()

    with pytest.raises(TypeError, match="feed takes bytes"):
        decoder.feed("id: 1\n\n")  # type: ignore[arg-type]


def _tool_input_nested(depth: int) -> dict[str, Any]:
    tool_input: dict[str, Any] = {"a": 1}
    for _ in range(depth - 1):
        tool_input = {"a": tool_input}
    return tool_input


def test_tool_input_at_the_depth_limit_survives_encoding_and_decoding() -> None:
    deep_call = call(1, "call-1", tool_input=_tool_input_nested(MAX_TOOL_INPUT_DEPTH))

    opening, decoded = RunStreamDecoder().feed(OPENING + encode_event(deep_call))

    assert opening == A_RUN[0]
    assert decoded == deep_call


def test_tool_input_deeper_than_the_limit_is_not_built() -> None:
    too_deep = _tool_input_nested(MAX_TOOL_INPUT_DEPTH + 1)

    with pytest.raises(ValidationError):
        call(1, "call-1", tool_input=too_deep)


def _frame_nested(depth: int) -> bytes:
    shallow_call = encode_event(call(1, "call-1", tool_input={"a": 1}))
    deep_frame = shallow_call.replace(b'{"a":1}', b'{"a":' * depth + b"1" + b"}" * depth)
    assert len(deep_frame) < MAX_EVENT_BYTES
    return deep_frame


def _violation_of(frame: bytes) -> StreamViolation:
    opening, violation = RunStreamDecoder().feed(OPENING + frame)

    assert opening == A_RUN[0]
    assert isinstance(violation, StreamViolation)
    return violation


def test_a_frame_whose_tool_input_nests_past_the_limit_is_a_schema_mismatch() -> None:
    violation = _violation_of(_frame_nested(MAX_TOOL_INPUT_DEPTH + 1))

    assert violation.kind is ViolationKind.SCHEMA_MISMATCH


def test_a_frame_nested_far_past_the_parser_stack_ends_in_a_violation() -> None:
    violation = _violation_of(_frame_nested(100_000))

    assert violation.kind in {ViolationKind.INVALID_JSON, ViolationKind.SCHEMA_MISMATCH}


def test_a_frame_the_json_parser_cannot_recurse_into_is_invalid_json(monkeypatch) -> None:
    def exhausted(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError

    decoder = RunStreamDecoder()
    assert decoder.feed(OPENING) == [A_RUN[0]]
    monkeypatch.setattr(sse_module.json, "loads", exhausted)

    [violation] = decoder.feed(_frame_nested(MAX_TOOL_INPUT_DEPTH + 1))

    assert isinstance(violation, StreamViolation)
    assert violation.kind is ViolationKind.INVALID_JSON


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param(ENCODED, id="a complete run"),
        pytest.param(OPENING, id="a run cut before its terminal event"),
    ],
)
def test_a_closed_decoder_refuses_a_second_close(stream: bytes) -> None:
    decoder = RunStreamDecoder()
    decoder.feed(stream)
    decoder.close()

    with pytest.raises(DecoderClosed):
        decoder.close()


@pytest.mark.parametrize(
    "cut",
    [
        pytest.param(len(encode_event(A_RUN[-1])), id="before the terminal frame"),
        pytest.param(1, id="inside the terminal frame"),
    ],
)
def test_a_stream_cut_before_its_terminal_event_is_truncated(cut: int) -> None:
    *decoded, violation = _decode(ENCODED[:-cut])

    assert decoded == A_RUN[:-1]
    assert isinstance(violation, StreamViolation)
    assert violation.kind is ViolationKind.TRUNCATED


def _text_of_frame_size(size: int) -> AssistantText:
    overhead = len(encode_event(text(1, "x"))) - 1
    return text(1, "x" * (size - overhead))


def test_a_frame_at_the_limit_travels_both_ways() -> None:
    event = _text_of_frame_size(MAX_EVENT_BYTES)
    frame = encode_event(event)

    assert len(frame) == MAX_EVENT_BYTES
    assert RunStreamDecoder().feed(OPENING + frame) == [A_RUN[0], event]


def test_a_frame_one_byte_over_the_limit_is_refused_both_ways() -> None:
    at_limit = encode_event(_text_of_frame_size(MAX_EVENT_BYTES))
    over_limit = at_limit.replace(b'"text":"', b'"text":"x', 1)

    oversized_event = _text_of_frame_size(MAX_EVENT_BYTES + 1)

    with pytest.raises(EventTooLarge):
        encode_event(oversized_event)
    *_, violation = RunStreamDecoder().feed(OPENING + over_limit)
    assert isinstance(violation, StreamViolation)
    assert violation.kind is ViolationKind.OVERSIZED_FRAME


def test_the_decoder_refuses_a_line_before_it_outgrows_the_limit() -> None:
    decoder = RunStreamDecoder()
    decoder.feed(OPENING)

    assert decoder.feed(b"data: " + b"x" * (MAX_EVENT_BYTES - len(b"data: "))) == []
    (violation,) = decoder.feed(b"x")
    assert violation.kind is ViolationKind.OVERSIZED_FRAME


def _base64_bytes(file: RunFile) -> int:
    return len(base64.b64encode(file.content))


def _written(event: Succeeded) -> bytes:
    # Written by hand only so a frame the encoder refuses can still reach the decoder.
    return f"id: {event.seq}\nevent: succeeded\ndata: {event.model_dump_json()}\n\n".encode()


def _pieces(frame: bytes, size: int = 65_536) -> Iterator[bytes]:
    for position in range(min(64, len(frame))):
        yield frame[position : position + 1]
    for position in range(64, len(frame), size):
        yield frame[position : position + size]


def _decode_split(frame: bytes) -> list[Decoded]:
    decoder = RunStreamDecoder()
    decoded = decoder.feed(OPENING)
    for piece in _pieces(frame):
        decoded += decoder.feed(piece)
        if decoded and isinstance(decoded[-1], StreamViolation):
            return decoded
    return decoded + decoder.close()


def _succeeded_with_non_file_part(size: int, files: tuple[RunFile, ...]) -> Succeeded:
    bare = succeeded(1, "x", files)
    non_file = len(encode_event(bare)) - sum(_base64_bytes(file) for file in files)
    return succeeded(1, "x" * (size - non_file + 1), files)


def _non_file_part(encoded: bytes, event: Succeeded) -> int:
    return len(encoded) - sum(_base64_bytes(file) for file in event.files)


def _a_file_of(size: int, name: str = "big") -> RunFile:
    return RunFile(name=name, content=bytes(size))


@pytest.fixture(scope="module")
def ceiling_frame() -> tuple[Succeeded, bytes]:
    event = succeeded(1, "", (_a_file_of(MAX_RUN_FILES_BYTES),))
    return event, encode_event(event)


def test_a_run_with_files_round_trips_whole_and_in_splits() -> None:
    event = succeeded(1, "Done.", (_a_file_of(100_000, "b"), RunFile(name="a", content=b"")))
    frame = encode_event(event)

    assert _decode(OPENING, frame)[1] == event
    assert _decode_split(frame)[1] == event


def test_files_of_exactly_the_ceiling_travel_both_ways(
    ceiling_frame: tuple[Succeeded, bytes],
) -> None:
    event, frame = ceiling_frame

    assert MAX_EVENT_BYTES < len(frame) <= MAX_SUCCEEDED_FRAME_BYTES
    assert _decode(OPENING, frame)[1] == event
    assert _decode_split(frame)[1] == event


def test_the_non_file_part_may_be_exactly_the_event_limit_and_no_more() -> None:
    files = (_a_file_of(1_000),)
    at_limit = _succeeded_with_non_file_part(MAX_EVENT_BYTES, files)
    over_limit = _succeeded_with_non_file_part(MAX_EVENT_BYTES + 1, files)

    assert _non_file_part(encode_event(at_limit), at_limit) == MAX_EVENT_BYTES
    assert _decode(OPENING, encode_event(at_limit))[1] == at_limit
    assert _decode_split(encode_event(at_limit))[1] == at_limit
    with pytest.raises(EventTooLarge, match=str(MAX_EVENT_BYTES)):
        encode_event(over_limit)
    for decoded in (_decode(OPENING, _written(over_limit)), _decode_split(_written(over_limit))):
        assert decoded[-1].kind is ViolationKind.OVERSIZED_FRAME
        assert str(MAX_EVENT_BYTES) in decoded[-1].detail


def test_files_that_base64_padding_pushes_over_the_frame_ceiling_are_refused_both_ways() -> None:
    # One byte and the rest less one: together exactly the ceiling, yet each pads on its own.
    files = (_a_file_of(1, "small"), _a_file_of(MAX_RUN_FILES_BYTES - 1, "big"))
    event = _succeeded_with_non_file_part(MAX_EVENT_BYTES, files)
    frame = _written(event)

    assert len(frame) > MAX_SUCCEEDED_FRAME_BYTES
    with pytest.raises(EventTooLarge, match=str(MAX_SUCCEEDED_FRAME_BYTES)):
        encode_event(event)
    for decoded in (_decode(OPENING, frame), _decode_split(frame)):
        assert decoded[-1].kind is ViolationKind.OVERSIZED_FRAME
        assert str(MAX_SUCCEEDED_FRAME_BYTES) in decoded[-1].detail


def _frame_breaking_the_schema_beyond_the_event_limit() -> bytes:
    # Fits under the frame ceiling, but its words alone fill the event limit, and
    # its seq of -1 breaks the schema as well.
    event = {
        "v": PROTOCOL_VERSION,
        "seq": -1,
        "type": "succeeded",
        "text": "x" * MAX_EVENT_BYTES,
        "files": [],
    }
    return f"id: -1\nevent: succeeded\ndata: {json.dumps(event)}\n\n".encode()


@pytest.mark.parametrize("decode", [lambda frame: _decode(OPENING, frame), _decode_split])
def test_a_succeeded_frame_over_the_event_limit_without_files_is_oversized_before_its_schema(
    decode: Any,
) -> None:
    violation = decode(_frame_breaking_the_schema_beyond_the_event_limit())[-1]

    assert violation.kind is ViolationKind.OVERSIZED_FRAME
    assert str(MAX_EVENT_BYTES) in violation.detail


@pytest.mark.parametrize("make", [lambda: heartbeat(2), lambda: text(2, "words")])
def test_another_frame_stays_at_the_event_limit_right_after_a_large_succeeded(make: Any) -> None:
    large = encode_event(succeeded(1, "", (_a_file_of(2 * MAX_EVENT_BYTES),)))
    padding = b'"seq":2,"pad":"' + b"x" * MAX_EVENT_BYTES + b'"'
    oversized = encode_event(make()).replace(b'"seq":2', padding)
    decoder = RunStreamDecoder()

    assert [type(item) for item in decoder.feed(OPENING + large)] == [type(A_RUN[0]), Succeeded]
    (violation,) = decoder.feed(oversized)
    assert violation.kind is ViolationKind.OVERSIZED_FRAME
    assert str(MAX_EVENT_BYTES) in violation.detail


def test_a_large_succeeded_whose_data_precedes_its_event_is_oversized() -> None:
    frame = encode_event(succeeded(1, "", (_a_file_of(2 * MAX_EVENT_BYTES),)))
    id_line, event_line, data_line, *_ = frame.split(b"\n")
    reordered = b"\n".join([id_line, data_line, event_line, b"", b""])

    for decoded in (_decode(OPENING, reordered), _decode_split(reordered)):
        assert decoded[-1].kind is ViolationKind.OVERSIZED_FRAME
        assert str(MAX_EVENT_BYTES) in decoded[-1].detail
