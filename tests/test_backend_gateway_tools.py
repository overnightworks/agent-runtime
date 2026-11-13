"""``GatewayBackend`` running an app's hand tools: the rows of the tool-loop matrix (M-L2)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections import UserString
from types import SimpleNamespace

import pytest

from agents.backend import TurnStopKind, TurnStopped
from agents.backend.fake_gateway import (
    FakeGateway,
    Reply,
    completion,
    tool_call,
    tool_round,
)
from agents.backend.gateway import GatewayBackend
from agents.tools import (
    ToolEffect,
    ToolOutcome,
)
from agents.turn import (
    MAX_EVENT_BYTES,
    MAX_TOOL_INPUT_DEPTH,
    TOOL_IMAGE_MEDIA_TYPE,
    FailureCode,
    RejectionCode,
)
from agents.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import (
    TURN_BUDGET_SECONDS,
    a_person,
    a_tool_request,
    ending,
    sent,
    shop_turn,
)
from tests.support.tool_images import a_tool_image

shop = gateway_turn_support.shop

PROVIDER_ERROR = (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)
LIMIT_EXCEEDED = (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
BUSY = (TurnStopKind.REFUSED, RejectionCode.BUSY)


def call_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.INFO and "lookup" in r.getMessage()]


def a_look(text: str, image, *, is_error: bool = False):
    """The answer of a tool that lets the agent see."""

    async def answer() -> ToolOutcome:
        return ToolOutcome(text, is_error=is_error, image=image)

    return answer


def read_data_url(url: str) -> tuple[str, bytes]:
    """The media type and the bytes a ``data:`` URL carries."""
    head, payload = url.removeprefix("data:").split(",", 1)
    media_type, encoding = head.split(";")
    assert encoding == "base64"
    return media_type, base64.b64decode(payload, validate=True)


def logged_flag(caplog) -> str:
    """The is_error state the one INFO record of the call states, as printed."""
    (record,) = call_records(caplog)
    return record.getMessage().rsplit("is_error=", 1)[1]


def test_one_round_runs_the_call_then_the_answer_follows(shop):
    shop.add_tool("lookup")

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {"topic": "x"})), completion("Done.")
    )

    assert stopped is None
    assert events == [
        ToolCallEvent(tool_use_id="c1", name="lookup", input={"topic": "x"}),
        ToolResultEvent(tool_use_id="c1", content="noted", is_error=False),
        AssistantTextEvent(text="Done."),
        FinalEvent(text="Done."),
    ]
    assert [(name, arguments) for name, _, arguments in shop.seen] == [("lookup", {"topic": "x"})]
    assert shop.seen[0][1] == a_person().principal
    follow_up = sent(gateway, 1)["messages"][-2:]
    assert follow_up[0]["tool_calls"][0]["id"] == "c1"
    assert follow_up[1] == {"role": "tool", "tool_call_id": "c1", "content": "noted"}


def test_two_rounds_run_both_calls_before_the_answer(shop):
    shop.add_tool("lookup")

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {})),
        tool_round(tool_call("c2", "lookup", {})),
        completion("Done."),
    )

    assert stopped is None
    assert [type(event) for event in events] == [
        ToolCallEvent,
        ToolResultEvent,
        ToolCallEvent,
        ToolResultEvent,
        AssistantTextEvent,
        FinalEvent,
    ]
    assert len(gateway.requests) == 3


def test_two_calls_in_one_round_run_in_order_each_between_its_events(shop):
    shop.add_tool("first")
    shop.add_tool("second")

    events, _, _ = shop.run(
        tool_round(tool_call("c1", "first", {}), tool_call("c2", "second", {})), completion("ok")
    )

    assert [(type(event), event.tool_use_id) for event in events[:4]] == [
        (ToolCallEvent, "c1"),
        (ToolResultEvent, "c1"),
        (ToolCallEvent, "c2"),
        (ToolResultEvent, "c2"),
    ]
    assert [name for name, _, _ in shop.seen] == ["first", "second"]


_NESTED_PAST_THE_DEPTH = '{"a": ' + "[" * MAX_TOOL_INPUT_DEPTH + "]" * MAX_TOOL_INPUT_DEPTH + "}"


@pytest.mark.parametrize(
    "bad_round",
    [
        tool_round(tool_call("c1", "lookup", {}), tool_call("c2", "unoffered", {})),
        tool_round(tool_call("c1", "lookup", "{not json")),
        tool_round(tool_call("c1", "lookup", "[]")),
        tool_round(tool_call("c1", "lookup", _NESTED_PAST_THE_DEPTH)),
        tool_round(tool_call("c1", "lookup", '{"a": NaN}')),
        tool_round(tool_call("c1", "lookup", '{"a": "\\ud800"}')),
        tool_round(tool_call("c1", "lookup", {}), tool_call("c1", "lookup", {})),
        tool_round(),
        tool_round(tool_call("\ud800", "lookup", {})),
        tool_round(tool_call("c1", "lookup", {}), tool_call("\ud800", "lookup", {})),
        tool_round(tool_call("c1", "lookup", {}), content="lone \ud800 surrogate"),
    ],
    ids=[
        "unoffered",
        "not-json",
        "not-an-object",
        "arguments-nested-past-the-depth",
        "arguments-not-finite",
        "arguments-lone-surrogate",
        "duplicate-id",
        "no-calls",
        "surrogate-id",
        "surrogate-id-second-call",
        "surrogate-content",
    ],
)
def test_a_round_that_is_not_whole_runs_nothing_and_shows_nothing(shop, bad_round):
    shop.add_tool("lookup", effect=ToolEffect.SIDE_EFFECTING)

    events, stopped, gateway = shop.run(bad_round, completion("never"))

    assert ending(stopped) == PROVIDER_ERROR
    assert not stopped.after_side_effect
    assert (shop.seen, events, len(gateway.requests)) == ([], [], 1)


def test_a_call_to_a_tool_the_agent_did_not_select_is_refused(shop):
    shop.add_tool("lookup")
    shop.add_tool("erase", effect=ToolEffect.SIDE_EFFECTING)

    events, stopped, _ = shop.run(
        tool_round(tool_call("c1", "erase", {})), completion("never"), selected=("lookup",)
    )

    assert (ending(stopped), shop.seen, events) == (PROVIDER_ERROR, [], [])


def test_an_error_outcome_reaches_the_model_and_the_turn_goes_on(shop):
    async def failing() -> ToolOutcome:
        return ToolOutcome("no such note", is_error=True)

    shop.add_tool("lookup", answer=failing)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {})), completion("Sorry.")
    )

    assert stopped is None
    assert events[1] == ToolResultEvent(tool_use_id="c1", content="no such note", is_error=True)
    assert "no such note" in sent(gateway, 1)["messages"][-1]["content"]
    assert events[-1] == FinalEvent(text="Sorry.")


def test_the_model_reads_an_error_outcome_differently_from_a_success_of_the_same_text(shop):
    outcomes = iter([ToolOutcome("same", is_error=True), ToolOutcome("same", is_error=False)])

    async def answer() -> ToolOutcome:
        return next(outcomes)

    shop.add_tool("lookup", answer=answer)

    _, _, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {})),
        tool_round(tool_call("c2", "lookup", {})),
        completion("ok"),
    )

    results = [m for m in sent(gateway, 2)["messages"] if m["role"] == "tool"]
    assert results[0]["content"] != results[1]["content"]


@pytest.mark.parametrize(
    "unusable",
    [
        ToolOutcome("bad \udc80 name", is_error=False),
        ToolOutcome(UserString("ok"), is_error=False),
        None,
        ToolOutcome("ok", is_error=None),
        ToolOutcome("ok", is_error="maybe"),
        SimpleNamespace(content="done"),
    ],
    ids=[
        "lone-surrogate",
        "content-not-text",
        "no-outcome",
        "error-flag-none",
        "error-flag-text",
        "no-error-flag",
    ],
)
def test_an_unusable_outcome_of_a_side_effecting_handler_is_an_error_and_the_turn_goes_on(
    shop, unusable
):
    async def answer() -> ToolOutcome:
        return unusable

    shop.add_tool("write", effect=ToolEffect.SIDE_EFFECTING, answer=answer)

    events, stopped, _ = shop.run(
        tool_round(tool_call("c1", "write", {})), Reply(429), max_tool_calls=3
    )

    assert len(shop.seen) == 1
    assert [type(event) for event in events] == [ToolCallEvent, ToolResultEvent]
    assert events[1].is_error
    assert ending(stopped) == BUSY
    assert stopped.after_side_effect


def test_a_raising_handler_logs_its_class_only_and_the_model_gets_an_error(shop, caplog):
    async def raising() -> ToolOutcome:
        raise KeyError("secret-note-title")

    shop.add_tool("lookup", answer=raising)
    caplog.set_level(logging.DEBUG)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {})), completion("Sorry.")
    )

    assert stopped is None
    assert events[1].is_error
    assert logged_flag(caplog) == "True"
    assert "secret-note-title" not in events[1].content
    assert "secret-note-title" not in gateway.requests[1].content.decode()
    assert "KeyError" in caplog.text
    assert "secret-note-title" not in caplog.text
    assert "Traceback" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_a_handler_that_stops_the_turn_ends_it_with_that_exception_and_no_further_round(
    shop, caplog
):
    ran = []
    refusal = TurnStopped(
        TurnStopKind.REFUSED, RejectionCode.BUSY, retry_after_s=7, detail="credentials"
    )

    async def refusing() -> ToolOutcome:
        raise refusal

    async def later() -> ToolOutcome:
        ran.append("later")
        return ToolOutcome("noted", is_error=False)

    shop.add_tool("lookup", answer=refusing)
    shop.add_tool("other", answer=later)
    caplog.set_level(logging.INFO)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {}), tool_call("c2", "other", {})),
        completion("never"),
    )

    assert stopped is refusal
    assert (stopped.kind, stopped.code) == (TurnStopKind.REFUSED, RejectionCode.BUSY)
    assert (stopped.retry_after_s, stopped.detail) == (7, "credentials")
    assert [type(event) for event in events] == [ToolCallEvent]
    assert ran == []
    assert len(gateway.requests) == 1
    assert logged_flag(caplog) == "None"


@pytest.mark.parametrize(
    ("effect", "handler_flag", "expected"),
    [
        (ToolEffect.SIDE_EFFECTING, False, True),
        (ToolEffect.READ_ONLY, False, False),
        (ToolEffect.READ_ONLY, True, True),
    ],
)
def test_a_handler_that_stops_the_turn_leaves_after_side_effect_as_the_connector_promises(
    shop, effect, handler_flag, expected
):
    async def refusing() -> ToolOutcome:
        raise TurnStopped(
            TurnStopKind.REFUSED, RejectionCode.FORBIDDEN, after_side_effect=handler_flag
        )

    shop.add_tool("lookup", effect=effect, answer=refusing)

    _, stopped, _ = shop.run(tool_round(tool_call("c1", "lookup", {})), completion("never"))

    assert stopped.after_side_effect is expected


@pytest.mark.parametrize("image", [None, a_tool_image()], ids=["text", "image"])
def test_each_call_logs_its_name_id_and_error_flag_never_arguments_or_content(shop, caplog, image):
    shop.add_tool("lookup", answer=a_look("content-canary", image, is_error=True))
    caplog.set_level(logging.DEBUG)

    shop.run(tool_round(tool_call("c1", "lookup", {"k": "argument-canary"})), completion("ok"))

    calls = call_records(caplog)
    assert len(calls) == 1
    assert "c1" in calls[0].getMessage()
    assert "True" in calls[0].getMessage()
    assert "argument-canary" not in caplog.text
    assert "content-canary" not in caplog.text
    if image is not None:
        assert base64.b64encode(image.content).decode("ascii") not in caplog.text
        assert repr(image.content) not in caplog.text


@pytest.mark.parametrize(
    ("replies", "calls_run"),
    [
        ((tool_round(tool_call("c1", "lookup", {}), tool_call("c2", "lookup", {})),), 0),
        (
            (
                tool_round(tool_call("c1", "lookup", {})),
                tool_round(tool_call("c2", "lookup", {})),
            ),
            1,
        ),
    ],
    ids=["one-round", "across-rounds"],
)
def test_crossing_max_tool_calls_fails_with_limit_exceeded(shop, replies, calls_run):
    shop.add_tool("lookup")

    _, stopped, _ = shop.run(*replies, completion("never"), max_tool_calls=1)

    assert ending(stopped) == LIMIT_EXCEEDED
    assert len(shop.seen) == calls_run


@pytest.mark.parametrize("effect", list(ToolEffect))
def test_a_deadline_during_a_handler_cancels_it_and_keeps_the_side_effect_state(
    shop, effect, caplog
):
    cancelled = []

    async def stuck() -> ToolOutcome:
        asyncio.get_running_loop().skew_seconds += TURN_BUDGET_SECONDS + 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return ToolOutcome("never", is_error=False)

    shop.add_tool("lookup", effect=effect, answer=stuck)
    caplog.set_level(logging.INFO)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {})), completion("never")
    )

    assert ending(stopped) == LIMIT_EXCEEDED
    assert stopped.after_side_effect is (effect is ToolEffect.SIDE_EFFECTING)
    assert cancelled == [True]
    assert [type(event) for event in events] == [ToolCallEvent]
    assert len(gateway.requests) == 1
    assert logged_flag(caplog) == "None"


def test_a_call_event_taken_past_the_deadline_starts_no_handler_and_leaves_no_record(shop, caplog):
    shop.add_tool("write", effect=ToolEffect.SIDE_EFFECTING)
    caplog.set_level(logging.INFO)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "write", {})),
        completion("never"),
        late_after=ToolCallEvent,
    )

    assert ending(stopped) == LIMIT_EXCEEDED
    assert not stopped.after_side_effect
    assert (shop.seen, [type(event) for event in events], len(gateway.requests)) == (
        [],
        [ToolCallEvent],
        1,
    )
    assert [r for r in caplog.records if "write" in r.getMessage()] == []


def test_a_deadline_that_passed_while_the_app_took_the_result_event_sends_no_next_request(shop):
    shop.add_tool("write", effect=ToolEffect.SIDE_EFFECTING)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "write", {})),
        completion("never"),
        late_after=ToolResultEvent,
    )

    assert ending(stopped) == LIMIT_EXCEEDED
    assert stopped.after_side_effect
    assert ([type(event) for event in events], len(gateway.requests)) == (
        [ToolCallEvent, ToolResultEvent],
        1,
    )


def test_a_call_id_reused_in_a_later_round_refuses_that_round_and_keeps_the_side_effect_state(
    shop,
):
    shop.add_tool("write", effect=ToolEffect.SIDE_EFFECTING)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "write", {})),
        tool_round(tool_call("c1", "write", {})),
        completion("never"),
    )

    assert ending(stopped) == PROVIDER_ERROR
    assert stopped.after_side_effect
    assert len(shop.seen) == 1
    assert [type(event) for event in events] == [ToolCallEvent, ToolResultEvent]
    assert len(gateway.requests) == 2


@pytest.mark.parametrize(
    ("effect", "raises", "wrote"),
    [
        (ToolEffect.READ_ONLY, False, False),
        (ToolEffect.SIDE_EFFECTING, False, True),
        (ToolEffect.SIDE_EFFECTING, True, True),
    ],
    ids=["read-only", "side-effecting", "side-effecting-that-raised"],
)
def test_a_busy_gateway_at_round_two_reports_whether_a_call_may_have_written(
    shop, effect, raises, wrote
):
    async def answer() -> ToolOutcome:
        if raises:
            raise RuntimeError("boom")
        return ToolOutcome("noted", is_error=False)

    shop.add_tool("lookup", effect=effect, answer=answer)

    _, stopped, _ = shop.run(
        tool_round(tool_call("c1", "lookup", {})), Reply(429, {"Retry-After": "7"})
    )

    assert ending(stopped) == BUSY
    assert stopped.after_side_effect is wrote
    assert stopped.retry_after_s == 7


def test_cancelling_between_a_call_and_its_result_reaches_the_caller_unchanged(shop, caplog):
    started = asyncio.Event()

    async def stuck() -> ToolOutcome:
        started.set()
        await asyncio.Event().wait()
        return ToolOutcome("never", is_error=False)

    shop.add_tool("lookup", effect=ToolEffect.SIDE_EFFECTING, answer=stuck)
    gateway = FakeGateway(tool_round(tool_call("c1", "lookup", {})), completion("never"))
    backend = GatewayBackend(shop.config(), tools=shop.tools, transport=gateway)
    events: list = []
    caplog.set_level(logging.INFO)

    async def consume() -> None:
        request = a_tool_request(shop.tools, 3)
        async with contextlib.aclosing(backend.run(request, a_person())) as stream:
            async for event in stream:
                events.append(event)

    async def cancel_while_running() -> None:
        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_while_running())

    assert [type(event) for event in events] == [ToolCallEvent]
    assert len(gateway.requests) == 1
    assert logged_flag(caplog) == "None"


@pytest.mark.parametrize(
    ("extra_bytes", "reaches_the_model"), [(0, True), (1, False)], ids=["at-bound", "one-over"]
)
def test_a_tool_result_over_the_event_bound_is_replaced_and_the_side_effect_stands(
    shop, caplog, extra_bytes, reaches_the_model
):
    caplog.set_level(logging.INFO)

    async def big() -> ToolOutcome:
        return ToolOutcome("a" * (MAX_EVENT_BYTES + extra_bytes), is_error=False)

    shop.add_tool("write", effect=ToolEffect.SIDE_EFFECTING, answer=big)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "write", {})), Reply(429), max_tool_calls=3
    )

    result = events[1]
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert [f"is_error={result.is_error}" in line for line in logged] == [True]
    assert (result.content == "a" * MAX_EVENT_BYTES) is reaches_the_model
    assert result.is_error is (not reaches_the_model)
    assert result.content in sent(gateway, 1)["messages"][-1]["content"]
    assert len(shop.seen) == 1
    assert stopped.after_side_effect


def test_repeated_large_results_stop_the_turn_before_a_request_over_the_bound(shop):
    async def big() -> ToolOutcome:
        return ToolOutcome("a" * MAX_EVENT_BYTES, is_error=False)

    shop.add_tool("write", effect=ToolEffect.SIDE_EFFECTING, answer=big)

    rounds = [tool_round(tool_call(f"c{number}", "write", {})) for number in range(8)]
    events, stopped, gateway = shop.run(*rounds, max_tool_calls=100)

    assert ending(stopped) == LIMIT_EXCEEDED
    assert stopped.after_side_effect
    assert max(len(request.content) for request in gateway.requests) <= 4 * MAX_EVENT_BYTES
    assert len(gateway.requests) == len(shop.seen)
    assert len(events) == 2 * len(shop.seen)


def tool_messages_of(gateway, request_index: int) -> list[dict]:
    return [m for m in sent(gateway, request_index)["messages"] if m["role"] == "tool"]


def tool_message_of(gateway, request_index: int) -> dict:
    (message,) = tool_messages_of(gateway, request_index)
    return message


def test_an_image_result_reaches_the_model_as_an_image_part_inside_its_tool_message(shop):
    image = a_tool_image()
    shop.add_tool("look", answer=a_look("a doorway", image))

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "look", {})), completion("A doorway.")
    )

    assert stopped is None
    assert events[1] == ToolResultEvent(tool_use_id="c1", content="a doorway", image=image)
    message = tool_message_of(gateway, 1)
    assert (message["role"], message["tool_call_id"]) == ("tool", "c1")
    image_part, text_part = message["content"]
    assert (image_part["type"], text_part) == ("image_url", {"type": "text", "text": "a doorway"})
    assert read_data_url(image_part["image_url"]["url"]) == (TOOL_IMAGE_MEDIA_TYPE, image.content)


@pytest.mark.parametrize("text", ["", " \n\t"], ids=["empty", "blank"])
def test_an_image_with_no_text_is_sent_as_the_image_part_alone(shop, text):
    image = a_tool_image()
    shop.add_tool("look", answer=a_look(text, image))

    _, stopped, gateway = shop.run(tool_round(tool_call("c1", "look", {})), completion("ok"))

    assert stopped is None
    (image_part,) = tool_message_of(gateway, 1)["content"]
    assert image_part["type"] == "image_url"
    assert read_data_url(image_part["image_url"]["url"])[1] == image.content


def test_an_error_outcome_with_an_image_sends_the_error_text_beside_it(shop):
    shop.add_tool("look", answer=a_look("no light", a_tool_image(), is_error=True))

    _, _, gateway = shop.run(tool_round(tool_call("c1", "look", {})), completion("ok"))

    image_part, text_part = tool_message_of(gateway, 1)["content"]
    assert image_part["type"] == "image_url"
    assert text_part == {"type": "text", "text": "Error: no light"}


def test_every_later_round_of_the_turn_resends_the_image(shop):
    shop.add_tool("look", answer=a_look("a doorway", a_tool_image()))
    shop.add_tool("note")

    _, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "look", {})),
        tool_round(tool_call("c2", "note", {})),
        completion("ok"),
    )

    assert stopped is None
    look, later_note = tool_messages_of(gateway, 2)
    assert look == tool_message_of(gateway, 1)
    assert look["content"][0]["type"] == "image_url"
    assert later_note["content"] == "noted"


@pytest.mark.parametrize(
    ("image_bytes", "text_bytes", "reaches_the_model"),
    [
        (MAX_EVENT_BYTES - 10, 10, True),
        (MAX_EVENT_BYTES - 10, 11, False),
        (MAX_EVENT_BYTES, 0, True),
    ],
    ids=["text and image at the bound", "text and image one over", "an image alone at the bound"],
)
def test_an_image_result_over_the_event_bound_is_replaced_and_the_side_effect_stands(
    shop, image_bytes, text_bytes, reaches_the_model
):
    image = a_tool_image(image_bytes)
    shop.add_tool(
        "look",
        effect=ToolEffect.SIDE_EFFECTING,
        answer=a_look("a" * text_bytes, image),
    )

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "look", {})), Reply(429), max_tool_calls=3
    )

    result = events[1]
    assert (result.image == image) is reaches_the_model
    assert result.is_error is (not reaches_the_model)
    assert isinstance(tool_message_of(gateway, 1)["content"], list) is reaches_the_model
    assert len(shop.seen) == 1
    assert stopped.after_side_effect


def test_two_full_size_images_and_a_third_stop_the_turn_before_a_request_over_the_bound(shop):
    shop.add_tool(
        "look",
        effect=ToolEffect.SIDE_EFFECTING,
        answer=a_look("", a_tool_image(MAX_EVENT_BYTES)),
    )

    rounds = [tool_round(tool_call(f"c{number}", "look", {})) for number in range(5)]
    _, stopped, gateway = shop.run(*rounds, max_tool_calls=100)

    assert ending(stopped) == LIMIT_EXCEEDED
    assert len(shop.seen) == 3
    assert len(gateway.requests) == 3
    assert max(len(request.content) for request in gateway.requests) <= 4 * MAX_EVENT_BYTES


def test_a_selected_tool_without_a_handler_raises_before_any_request(shop):
    shop.add_tool("lookup")
    gateway = FakeGateway(completion("never"))
    request = a_tool_request(shop.tools, 3)
    backend = GatewayBackend(shop.config(), tools=(), transport=gateway)
    turn = shop_turn(backend, request)

    with pytest.raises(ValueError, match="lookup"):
        asyncio.run(turn)

    assert gateway.requests == []


def test_two_tools_with_one_name_are_refused_at_construction(shop):
    shop.add_tool("lookup")
    shop.add_tool("lookup")
    config = shop.config()
    tools = shop.tools

    with pytest.raises(ValueError, match="lookup"):
        GatewayBackend(config, tools=tools)


def test_a_request_declaration_that_differs_from_its_handler_s_raises_before_any_request(shop):
    shop.add_tool("write", effect=ToolEffect.SIDE_EFFECTING)
    gateway = FakeGateway(completion("never"))
    declared_read_only = a_tool_request(shop.tools, 3).model_copy(
        update={
            "tools": (
                shop.tools[0].declaration.model_copy(update={"effect": ToolEffect.READ_ONLY}),
            )
        }
    )
    backend = GatewayBackend(shop.config(), tools=shop.tools, transport=gateway)
    turn = shop_turn(backend, declared_read_only)

    with pytest.raises(ValueError, match="write"):
        asyncio.run(turn)

    assert gateway.requests == []


def test_the_request_carries_only_model_messages_user_stream_options_and_tools_without_the_effect(
    shop,
):
    shop.add_tool("lookup", effect=ToolEffect.READ_ONLY)

    _, _, gateway = shop.run(completion("ok"))

    body = sent(gateway, 0)
    assert set(body) == {"model", "messages", "user", "stream", "stream_options", "tools"}
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "lookup", "description": "Use lookup.", "parameters": {}},
        }
    ]
