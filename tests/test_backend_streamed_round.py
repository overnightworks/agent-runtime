"""The streamed-round reader called directly: framing, fragments, errors and the bounds."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import aclosing

import pytest
from pydantic import ValidationError

from agents.backend.streamed_round import (
    MalformedRound,
    RoundErrorEvent,
    StreamedRound,
    read_round,
)
from agents.turn import MAX_EVENT_BYTES, TurnUsage
from tests.support.gateway_turn_support import a_chunk, a_fragment, a_turn_usage, an_event_stream

ROUND_LIMIT = 4 * MAX_EVENT_BYTES
DATA_BOUND = 6 * MAX_EVENT_BYTES + 65_536
ANSWER_AB = StreamedRound("stop", {"role": "assistant", "content": "ab", "tool_calls": []})


async def _arriving(chunks: Iterable[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _handed_out(chunks: Iterable[bytes], limit: int) -> list[str | StreamedRound]:
    async with aclosing(read_round(_arriving(chunks), round_limit_bytes=limit)) as parts:
        return [part async for part in parts]


def read(*chunks: bytes, limit: int = ROUND_LIMIT) -> StreamedRound:
    """The round the reader hands out last, after the round's text pieces."""
    *_, streamed = asyncio.run(_handed_out(chunks, limit))
    return streamed


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
        b"{}",
        b'{"choices": null}',
        b'{"usage": null}',
        b'{"choices": null, "usage": null}',
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
        "neither-choices-nor-usage",
        "choices-null",
        "usage-null-without-choices",
        "choices-null-and-usage-null",
        "not-utf-8",
        "lone-surrogate-in-a-piece",
        "nested-past-the-recursion-limit",
    ],
)
def test_an_event_that_is_no_chunk_is_malformed_with_no_context(data):
    before = an_event_stream(a_chunk("a", finish_reason="stop"), done=False)
    chunks = (before, b"data: " + data + b"\n\n", an_event_stream())

    with pytest.raises(MalformedRound) as raised:
        read(*chunks)

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


def _answer_with(*usage_chunks: dict) -> bytes:
    return an_event_stream(a_chunk("a"), a_chunk("b"), a_chunk(finish_reason="stop"), *usage_chunks)


def _usage_chunk(usage: object) -> dict:
    return {"choices": [], "usage": usage}


@pytest.mark.parametrize(
    ("usage", "counted"),
    [
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 30},
            },
            a_turn_usage(100, 7, cached_input_tokens=60, cache_write_tokens=30),
        ),
        (
            {"prompt_tokens": 100, "completion_tokens": 7},
            a_turn_usage(100, 7),
        ),
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": None},
                "cache_creation_input_tokens": None,
            },
            a_turn_usage(100, 7),
        ),
        (
            {"prompt_tokens": 100, "completion_tokens": 7, "cache_creation_input_tokens": 40},
            a_turn_usage(100, 7, cache_write_tokens=40),
        ),
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cache_write_tokens": 30},
                "cache_creation_input_tokens": 40,
            },
            a_turn_usage(100, 7, cache_write_tokens=30),
        ),
    ],
    ids=[
        "details-carry-both-cache-counts",
        "cache-counts-absent-are-zero",
        "cache-counts-null-are-zero",
        "top-level-cache-write-when-details-have-none",
        "details-cache-write-before-the-top-level-one",
    ],
)
def test_a_usage_chunk_with_no_choice_gives_the_rounds_usage(usage, counted):
    assert read(_answer_with(_usage_chunk(usage))).usage == counted


def test_usage_on_a_chunk_that_carries_a_choice_gives_the_rounds_usage():
    usage = {"prompt_tokens": 9, "completion_tokens": 2}
    finish = a_chunk(finish_reason="stop") | {"usage": usage}

    streamed = read(an_event_stream(a_chunk("ab"), finish))

    assert streamed.usage == a_turn_usage(9, 2)


@pytest.mark.parametrize(
    "choices_member",
    [{}, {"choices": None}],
    ids=["no-choices-key", "choices-null"],
)
def test_a_usage_chunk_without_choices_gives_the_rounds_usage(choices_member):
    usage = {"prompt_tokens": 9, "completion_tokens": 2}

    streamed = read(_answer_with(choices_member | {"usage": usage}))

    assert streamed.usage == a_turn_usage(9, 2)


def test_a_later_chunk_with_null_usage_keeps_the_usage_already_read():
    usage = {"prompt_tokens": 9, "completion_tokens": 2}

    streamed = read(_answer_with(_usage_chunk(usage), _usage_chunk(None)))

    assert streamed.usage == a_turn_usage(9, 2)


def test_a_round_with_no_usage_chunk_carries_no_usage():
    assert read(_answer_with()).usage is None


def test_a_turns_usage_refuses_a_count_assigned_after_it_was_made():
    usage = a_turn_usage(9, 2)

    with pytest.raises(ValidationError):
        usage.input_tokens = 10  # type: ignore[misc]


@pytest.mark.parametrize("requests", [None, 0, -1], ids=["missing", "zero", "negative"])
def test_a_turns_usage_refuses_a_missing_or_non_positive_request_count(requests):
    counts = {"input_tokens": 9, "cached_input_tokens": 0, "cache_write_tokens": 0}
    counts |= {"output_tokens": 2} | ({} if requests is None else {"requests": requests})

    with pytest.raises(ValidationError):
        TurnUsage.model_validate(counts)


@pytest.mark.parametrize(
    "usage",
    [
        "many",
        ["prompt_tokens"],
        5,
        True,
        {},
        {"completion_tokens": 7},
        {"prompt_tokens": 100},
        {"prompt_tokens": -1, "completion_tokens": 7},
        {"prompt_tokens": 100, "completion_tokens": -7},
        {"prompt_tokens": 1.5, "completion_tokens": 7},
        {"prompt_tokens": 100.0, "completion_tokens": 7},
        {"prompt_tokens": "100", "completion_tokens": 7},
        {"prompt_tokens": True, "completion_tokens": 7},
        {"prompt_tokens": 100, "completion_tokens": None},
        {"prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": "all"},
        {
            "prompt_tokens": 100,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": -2},
        },
        {
            "prompt_tokens": 100,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cache_write_tokens": True},
        },
        {"prompt_tokens": 100, "completion_tokens": 7, "cache_creation_input_tokens": "3"},
    ],
    ids=[
        "text",
        "list",
        "number",
        "boolean",
        "empty",
        "no-prompt-tokens",
        "no-completion-tokens",
        "negative-prompt-tokens",
        "negative-completion-tokens",
        "fractional-count",
        "float-count",
        "text-count",
        "boolean-count",
        "null-required-count",
        "details-not-an-object",
        "negative-cached-count",
        "boolean-cache-write-count",
        "text-top-level-cache-write-count",
    ],
)
def test_unreadable_usage_gives_a_round_with_no_usage_and_the_same_message(usage):
    streamed = read(_answer_with(_usage_chunk(usage)))

    assert streamed == ANSWER_AB
    assert streamed.usage is None
