"""What a turn's rounds ask for and what its final event reports: the prefix and the usage."""

from __future__ import annotations

import json

import pytest

from agents.backend import TurnStopKind
from agents.backend.fake_gateway import Reply, completion, tool_call, tool_round
from agents.turn import FailureCode, TurnUsage
from agents.turn_events import AssistantTextEvent, FinalEvent, ToolCallEvent, ToolResultEvent
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import a_chunk, an_event_stream, ending

shop = gateway_turn_support.shop


def usage_sent(prompt_tokens: int, completion_tokens: int, **details: int) -> dict:
    """The usage object a gateway sends for a round; the keywords are its prompt token details."""
    usage: dict = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if details:
        usage["prompt_tokens_details"] = details
    return usage


def run(shop, *replies: Reply, max_tool_calls: int = 5):
    """A turn of an agent with the tools ``lookup`` and ``note`` against the scripted gateway."""
    shop.add_tool("lookup")
    shop.add_tool("note")
    return shop.run(*replies, max_tool_calls=max_tool_calls)


def test_the_final_event_carries_the_usage_the_gateway_sent(shop):
    sent = usage_sent(120, 9, cached_tokens=80, cache_write_tokens=25)

    events, stopped, _ = run(shop, completion("The answer.", usage=sent))

    assert stopped is None
    assert events == [
        AssistantTextEvent(text="The answer."),
        FinalEvent(
            text="The answer.",
            usage=TurnUsage(
                input_tokens=120, cached_input_tokens=80, cache_write_tokens=25, output_tokens=9
            ),
        ),
    ]


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "many",
        {"prompt_tokens": 120},
        {"prompt_tokens": -1, "completion_tokens": 9},
        {"prompt_tokens": 1.5, "completion_tokens": 9},
        {"prompt_tokens": True, "completion_tokens": 9},
    ],
    ids=["none-sent", "not-an-object", "missing-count", "negative", "fractional", "boolean"],
)
def test_a_turn_whose_round_has_no_readable_usage_ends_with_no_usage_and_the_same_text(shop, usage):
    events, stopped, _ = run(shop, completion("The answer.", usage=usage))

    assert stopped is None
    assert events == [AssistantTextEvent(text="The answer."), FinalEvent(text="The answer.")]


def an_answer_closed_by(last_chunk: dict) -> Reply:
    """The text round ``ab``, its finish chunk, then ``last_chunk``, as the gateway streams them."""
    chunks = (a_chunk("ab"), a_chunk(finish_reason="stop"), last_chunk)
    return Reply(headers={"content-type": "text/event-stream"}, body=an_event_stream(*chunks))


@pytest.mark.parametrize(
    "last_chunk",
    [{"usage": usage_sent(120, 9)}, {"choices": None, "usage": usage_sent(120, 9)}],
    ids=["no-choices-key", "choices-null"],
)
def test_a_usage_chunk_without_choices_gives_the_turn_its_usage(shop, last_chunk):
    events, stopped, _ = run(shop, an_answer_closed_by(last_chunk))

    assert stopped is None
    assert events[-1] == FinalEvent(
        text="ab",
        usage=TurnUsage(
            input_tokens=120, cached_input_tokens=0, cache_write_tokens=0, output_tokens=9
        ),
    )


@pytest.mark.parametrize(
    "last_chunk",
    [{}, {"choices": None}],
    ids=["empty", "choices-null"],
)
def test_a_chunk_with_neither_choices_nor_usage_still_stops_the_turn(shop, last_chunk):
    _, stopped, _ = run(shop, an_answer_closed_by(last_chunk))

    assert ending(stopped) == (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)


@pytest.mark.parametrize(
    "last_chunk",
    [{"usage": None}, {"choices": None, "usage": None}],
    ids=["no-choices-key", "choices-null"],
)
def test_a_usage_chunk_with_a_null_usage_ends_the_turn_with_no_usage(shop, last_chunk):
    events, stopped, _ = run(shop, an_answer_closed_by(last_chunk))

    assert stopped is None
    assert events == [AssistantTextEvent(text="ab"), FinalEvent(text="ab")]


def raw_members(body: bytes) -> dict[str, str]:
    """Each top-level member of a JSON object, as the exact text the body wrote for it."""
    text, decoder = body.decode(), json.JSONDecoder()
    members: dict[str, str] = {}
    at = text.index("{") + 1
    while True:
        at += len(text[at:]) - len(text[at:].lstrip(" ,\n"))
        if text[at] == "}":
            return members
        key, at = decoder.raw_decode(text, at)
        at = text.index(":", at) + 1
        at += len(text[at:]) - len(text[at:].lstrip(" "))
        _, end = decoder.raw_decode(text, at)
        members[key] = text[at:end]
        at = end


def test_a_second_rounds_body_begins_with_the_first_rounds_model_tools_and_messages(shop):
    _, stopped, gateway = run(
        shop, tool_round(tool_call("c1", "lookup", {"topic": "x"})), completion("Done.")
    )

    first, second = (raw_members(request.content) for request in gateway.requests)
    assert stopped is None
    assert second["model"] == first["model"]
    assert second["tools"] == first["tools"]
    assert second["messages"].startswith(first["messages"].removesuffix("]"))
    assert len(json.loads(second["messages"])) > len(json.loads(first["messages"]))
    assert all("cache_control" not in request.content.decode() for request in gateway.requests)


def test_the_final_events_usage_sums_the_usage_of_every_round(shop):
    first_round = usage_sent(100, 5, cached_tokens=0, cache_write_tokens=90)
    last_round = usage_sent(130, 8, cached_tokens=90)

    events, stopped, _ = run(
        shop,
        tool_round(tool_call("c1", "lookup", {}), usage=first_round),
        completion("Done.", usage=last_round),
    )

    assert stopped is None
    assert events[-1].usage == TurnUsage(
        input_tokens=230, cached_input_tokens=90, cache_write_tokens=90, output_tokens=13
    )


@pytest.mark.parametrize(
    ("first", "last"),
    [
        (None, usage_sent(130, 8)),
        (usage_sent(100, 5), None),
        (usage_sent(100, 5), {"prompt_tokens": -1}),
    ],
    ids=["first-round-has-none", "last-round-has-none", "last-round-unreadable"],
)
def test_a_turn_with_a_round_that_has_no_usage_ends_with_no_usage(shop, first, last):
    events, stopped, _ = run(
        shop,
        tool_round(tool_call("c1", "lookup", {}), usage=first),
        completion("Done.", usage=last),
    )

    assert stopped is None
    assert events[-1] == FinalEvent(text="Done.")


def test_a_turn_that_stops_after_a_paid_round_yields_what_it_yields_without_usage(shop):
    events, stopped, _ = run(
        shop,
        tool_round(tool_call("c1", "lookup", {}), usage=usage_sent(100, 5)),
        tool_round(tool_call("c2", "lookup", {}), usage=usage_sent(130, 8)),
        max_tool_calls=1,
    )

    assert ending(stopped) == (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
    assert [type(event) for event in events] == [ToolCallEvent, ToolResultEvent]
