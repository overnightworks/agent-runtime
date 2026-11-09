"""The streamed-round reader called directly: framing, fragments, errors and the bounds."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable

import pytest

from agent_runtime_client.backend.streamed_round import (
    MalformedRound,
    RoundErrorEvent,
    StreamedRound,
    read_round,
)
from agent_runtime_client.turn import MAX_EVENT_BYTES
from tests.support.gateway_turn_support import a_chunk, a_fragment, an_event_stream

ROUND_LIMIT = 4 * MAX_EVENT_BYTES
DATA_BOUND = 6 * MAX_EVENT_BYTES + 65_536
ANSWER_AB = StreamedRound("stop", {"role": "assistant", "content": "ab", "tool_calls": []})


async def _arriving(chunks: Iterable[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def read(*chunks: bytes, limit: int = ROUND_LIMIT) -> StreamedRound:
    return asyncio.run(read_round(_arriving(chunks), round_limit_bytes=limit))


def _event(chunk: dict) -> str:
    return f"data: {json.dumps(chunk)}\n\n"


def _answer_ab() -> list[str]:
    return [_event(a_chunk("a")), _event(a_chunk("b")), _event(a_chunk(finish_reason="stop"))]


def _calls_round(*fragments: dict, reason: str = "tool_calls") -> bytes:
    chunks = [a_chunk(tool_calls=[fragment]) for fragment in fragments]
    return an_event_stream(*chunks, a_chunk(finish_reason=reason))


@pytest.mark.parametrize(
    "stream",
    [
        "".join(_answer_ab()) + "data: [DONE]\n\n",
        "".join(_answer_ab()).replace("\n", "\r\n") + "data: [DONE]\r\n\r\n",
        ": ping\n\n" + "".join(_answer_ab()) + ": ping\n\ndata: [DONE]\n\n",
        "".join(_answer_ab()) + "retry: 5\nid: 7\nevent: x\ndata: [DONE]\n\n",
        "﻿" + "".join(_answer_ab()) + "data: [DONE]\n\n",
        "".join(_answer_ab()).replace("data: ", "data:") + "data:[DONE]\n\n",
        _event({"choices": [{"delta": {"role": "assistant", "content": ""}}]})
        + _event({"choices": []})
        + "".join(_answer_ab())
        + "data: [DONE]\n\n",
        "data: "
        + json.dumps(a_chunk("ab")).replace(",", ",\ndata: ", 1)
        + "\n\n"
        + _event(a_chunk(finish_reason="stop"))
        + "data: [DONE]\n\n",
        "data: [DONE]\n\ndata: not read\n\n".join(["".join(_answer_ab()), ""]),
    ],
    ids=[
        "lf",
        "crlf",
        "comments",
        "other-fields",
        "bom",
        "no-space-after-the-colon",
        "role-only-and-empty-choices",
        "data-lines-joined-with-lf",
        "nothing-read-after-done",
    ],
)
def test_the_framing_gives_the_round_of_its_events(stream):
    assert read(stream.encode()) == ANSWER_AB


@pytest.mark.parametrize("size", [1, 2, 3, 7])
def test_a_stream_cut_into_chunks_anywhere_gives_the_same_round(size):
    events = _event(a_chunk("ä")) + _event(a_chunk(finish_reason="stop")) + "data: [DONE]\n\n"
    stream = events.encode()
    chunks = [stream[start : start + size] for start in range(0, len(stream), size)]

    assert read(*chunks).message["content"] == "ä"


def test_data_pending_at_the_end_is_discarded():
    stream = ("".join(_answer_ab()) + "data: [DONE]").encode()

    with pytest.raises(MalformedRound):
        read(stream)


def test_the_last_finish_reason_counts():
    chunks = an_event_stream(a_chunk("a", finish_reason="length"), a_chunk(finish_reason="stop"))

    assert read(chunks).finish_reason == "stop"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"null",
        b'{"choices": ["x"]}',
        b'{"choices": [{"delta": []}]}',
        b'{"choices": [{"delta": {"content": 5}}]}',
        b'{"choices": [{"delta": {"tool_calls": {}}}]}',
        b'{"choices": [{"finish_reason": 5}]}',
        b"\xff",
        json.dumps(a_chunk("\ud800")).encode(),
        b"[" * 200_000,
    ],
    ids=[
        "empty-data",
        "null",
        "choice-not-an-object",
        "delta-not-an-object",
        "content-not-text",
        "tool-calls-not-a-list",
        "finish-reason-not-text",
        "not-utf-8",
        "lone-surrogate-in-a-piece",
        "nested-past-the-recursion-limit",
    ],
)
def test_an_event_that_is_no_chunk_is_malformed_with_no_context(data):
    before = an_event_stream(a_chunk("a", finish_reason="stop"), done=False)

    with pytest.raises(MalformedRound) as raised:
        read(before, b"data: " + data + b"\n\n", an_event_stream())

    assert raised.value.__context__ is None


def test_fragments_assemble_by_index_with_their_arguments_in_pieces():
    fragments = [
        a_fragment(0, call_id="c0", name="first", arguments=""),
        a_fragment(1, call_id="c1", name="second", arguments='{"b":'),
        a_fragment(0, arguments='{"a":'),
        a_fragment(1, arguments="2}"),
        a_fragment(0, call_id="c0", name="first", arguments="1}"),
    ]

    streamed = read(_calls_round(*fragments))

    assert streamed == StreamedRound(
        "tool_calls",
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c0",
                    "type": "function",
                    "function": {"name": "first", "arguments": '{"a":1}'},
                },
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "second", "arguments": '{"b":2}'},
                },
            ],
        },
    )


def test_text_before_the_calls_is_the_rounds_content():
    chunks = an_event_stream(
        a_chunk("Looking"),
        a_chunk(" up", tool_calls=[a_fragment(0, call_id="c0", name="first", arguments="{}")]),
        a_chunk(finish_reason="tool_calls"),
    )

    assert read(chunks).message["content"] == "Looking up"


@pytest.mark.parametrize(
    "fragments",
    [
        [{"function": {"name": "n"}, "id": "c"}],
        [a_fragment(True, call_id="c", name="n")],
        [a_fragment(1, call_id="c", name="n")],
        [a_fragment(-1, call_id="c", name="n")],
        [a_fragment(0, name="n")],
        [a_fragment(0, call_id="c")],
        [a_fragment(0, call_id="c", name="n"), a_fragment(0, call_id="other")],
        [a_fragment(0, call_id="c", name="n"), a_fragment(0, name="other")],
        [a_fragment(0, call_id="c", name="n", arguments=5)],
        [a_fragment(0, call_id="\ud800", name="n")],
        ["not an object"],
        [{"index": 0, "id": "c", "function": "n"}],
    ],
    ids=[
        "no-index",
        "boolean-index",
        "index-gap-at-the-start",
        "negative-index",
        "first-fragment-without-id",
        "first-fragment-without-name",
        "second-id-for-one-index",
        "second-name-for-one-index",
        "arguments-not-text",
        "surrogate-id",
        "fragment-not-an-object",
        "function-not-an-object",
    ],
)
def test_fragments_that_do_not_assemble_are_malformed(fragments):
    stream = _calls_round(*fragments)

    with pytest.raises(MalformedRound):
        read(stream)


@pytest.mark.parametrize(
    "error",
    [
        {"message": "secret-message", "type": "API ERROR", "code": "500"},
        {"message": "secret-message", "type": "api_error", "code": 500},
        {"message": "secret-message"},
        "secret-message",
    ],
    ids=["type-fails-the-rule", "code-not-text", "message-only", "plain-text"],
)
@pytest.mark.parametrize("done", [False, True], ids=["alone", "then-done"])
def test_an_error_event_whose_type_or_code_fails_the_rule_carries_neither(error, done):
    stream = an_event_stream(a_chunk("a"), {"error": error}, done=done)

    with pytest.raises(RoundErrorEvent) as raised:
        read(stream)

    assert raised.value.kind == "no error type given"
    assert "secret-message" not in str(raised.value)


@pytest.mark.parametrize(
    ("text", "fits"),
    [
        ("a" * MAX_EVENT_BYTES, True),
        ("a" * (MAX_EVENT_BYTES + 1), False),
        ("ä" * (MAX_EVENT_BYTES // 2 + 1), False),
    ],
    ids=["at-the-bound", "one-byte-over", "bytes-not-characters-over"],
)
def test_a_rounds_text_stays_within_the_event_bound_in_whatever_pieces(text, fits):
    half = len(text) // 2
    stream = an_event_stream(
        a_chunk(text[:half]), a_chunk(text[half:]), a_chunk(finish_reason="stop")
    )

    if fits:
        assert read(stream).message["content"] == text
    else:
        with pytest.raises(MalformedRound):
            read(stream)


@pytest.mark.parametrize(("extra", "fits"), [(0, True), (1, False)], ids=["at-the-bound", "over"])
def test_one_events_joined_data_stays_within_the_data_bound(extra, fits):
    head = '{"choices": [{"delta": {}, "finish_reason": "stop"}'
    tail = "]}"
    padding = "\t" * (DATA_BOUND - len(head) - len(tail) - 2 + extra)
    stream = f"data: {head}\ndata: {padding}\ndata: {tail}\n\ndata: [DONE]\n\n".encode()

    if fits:
        assert read(stream).finish_reason == "stop"
    else:
        with pytest.raises(MalformedRound):
            read(stream)


@pytest.mark.parametrize(("extra", "fits"), [(0, True), (1, False)], ids=["at-the-bound", "over"])
def test_a_complete_line_stays_within_the_data_bound(extra, fits):
    comment = ":" + " " * (DATA_BOUND - 1 + extra)
    stream = (comment + "\n\n" + "".join(_answer_ab()) + "data: [DONE]\n\n").encode()

    if fits:
        assert read(stream) == ANSWER_AB
    else:
        with pytest.raises(MalformedRound):
            read(stream)


@pytest.mark.parametrize(
    ("limit", "fits"), [(100, True), (99, False)], ids=["at-the-bound", "over"]
)
def test_the_assembled_round_stays_within_the_limit_it_is_given(limit, fits):
    # text 10 + id 2 + name 3 + arguments 85
    chunks = an_event_stream(
        a_chunk("t" * 10, tool_calls=[a_fragment(0, call_id="c0", name="abc", arguments="")]),
        a_chunk(tool_calls=[a_fragment(0, arguments="x" * 40)]),
        a_chunk(tool_calls=[a_fragment(0, arguments="y" * 45)]),
        a_chunk(finish_reason="tool_calls"),
    )

    if fits:
        assert read(chunks, limit=limit).finish_reason == "tool_calls"
    else:
        with pytest.raises(MalformedRound):
            read(chunks, limit=limit)
