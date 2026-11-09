"""``GatewayBackend`` reading a round from an event stream: what a turn does with it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field

import httpx2
import pytest

from agent_runtime_client.backend import TurnStopKind, TurnStopped
from agent_runtime_client.backend.fake_gateway import FakeGateway, Reply, completion
from agent_runtime_client.backend.gateway import GatewayBackend, GatewayDetail
from agent_runtime_client.tools import HandTool, ToolDeclaration, ToolEffect, ToolOutcome
from agent_runtime_client.turn import MAX_EVENT_BYTES, FailureCode
from agent_runtime_client.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import (
    UNAVAILABLE,
    Streamed,
    _turn,
    a_chunk,
    a_fragment,
    a_request,
    an_event_stream,
    ending,
)
from tests.support.virtual_time import VirtualTimeLoop, in_virtual_time

scenario = gateway_turn_support.scenario

PROVIDER_ERROR = (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)
LIMIT_EXCEEDED = (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
STOPPED = a_chunk(finish_reason="stop")
CALLS_ASKED = a_chunk(finish_reason="tool_calls")


@dataclass
class Lookup:
    """The app's one tool, side-effecting; ``seen`` holds the arguments of every call it ran."""

    seen: list = field(default_factory=list)

    def tool(self) -> HandTool:
        async def handler(principal, arguments):
            self.seen.append(arguments)
            return ToolOutcome("noted", is_error=False)

        declaration = ToolDeclaration(
            name="lookup", description="Look up.", parameters={}, effect=ToolEffect.SIDE_EFFECTING
        )
        return HandTool(declaration, handler)


def drive(scenario, transport, lookup: Lookup | None = None, pull=_turn):
    lookup = lookup or Lookup()
    tool = lookup.tool()
    request = a_request(tools=(tool.declaration,), max_tool_calls=3)
    backend = GatewayBackend(scenario.config(), tools=[tool], transport=transport)
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        return runner.run(pull(backend, request, scenario.person()[0]))


async def _turn_pulled_per_task(backend, request, person):
    """The turn's events and its stop, each event pulled from a task of its own."""

    async def next_event(stream):
        return await anext(stream, None)

    events = []
    try:
        async with contextlib.aclosing(backend.run(request, person)) as stream:
            while (event := await asyncio.create_task(next_event(stream))) is not None:
                events.append(event)
    except TurnStopped as stopped:
        return events, stopped
    return events, None


def test_each_text_piece_comes_as_its_own_event_in_order_then_the_joined_answer(scenario):
    gateway = FakeGateway(completion("Hel", "lo ä", "!"))

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=gateway))

    assert stopped is None
    assert events == [
        AssistantTextEvent(text="Hel"),
        AssistantTextEvent(text="lo ä"),
        AssistantTextEvent(text="!"),
        FinalEvent(text="Hello ä!"),
    ]


def test_a_piece_reaches_the_app_while_its_round_is_still_open(scenario):
    held = Streamed((an_event_stream(a_chunk("Hel"), done=False),), hangs=True)
    backend = GatewayBackend(scenario.config(), transport=held)

    async def take_the_first_event():
        async with contextlib.aclosing(backend.run(a_request(), scenario.person()[0])) as events:
            return await anext(events), held.closed.is_set()

    assert in_virtual_time(take_the_first_event()) == (AssistantTextEvent(text="Hel"), False)


def _a_call(name: str, arguments: str) -> dict:
    return a_chunk(tool_calls=[a_fragment(0, call_id="c0", name=name, arguments=arguments)])


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        (an_event_stream(a_chunk("a"), done=False), ["a"]),
        (an_event_stream(a_chunk("a")), ["a"]),
        (an_event_stream(a_chunk("a"), "{not json", STOPPED), ["a"]),
        (an_event_stream(a_chunk("a"), a_chunk("b"), a_chunk(finish_reason="length")), ["a", "b"]),
        (an_event_stream(a_chunk("a"), STOPPED, done=False), ["a"]),
        (an_event_stream(a_chunk("  \n"), STOPPED), ["  \n"]),
        (an_event_stream(a_chunk("a"), CALLS_ASKED), ["a"]),
        (an_event_stream(a_chunk("a"), _a_call("unoffered", "{}"), CALLS_ASKED), ["a"]),
        (an_event_stream(a_chunk("a"), _a_call("lookup", "[]"), CALLS_ASKED), ["a"]),
        (
            an_event_stream(a_chunk("a" * (MAX_EVENT_BYTES - 1)), a_chunk("bb"), STOPPED),
            ["a" * (MAX_EVENT_BYTES - 1)],
        ),
    ],
    ids=[
        "cut-after-a-piece",
        "done-without-a-finish-reason",
        "malformed-event-after-a-piece",
        "length",
        "cut-after-the-finish-reason",
        "blank-content",
        "tool-calls-without-calls",
        "a-call-to-a-tool-not-offered",
        "arguments-not-an-object",
        "a-piece-crossing-the-text-bound",
    ],
)
def test_a_round_that_streams_text_then_fails_leaves_the_text_shown_and_runs_nothing(
    scenario, body, shown
):
    lookup = Lookup()
    transport = Streamed((body,))

    events, stopped = drive(scenario, transport, lookup)

    assert (events, ending(stopped), lookup.seen) == (
        [AssistantTextEvent(text=piece) for piece in shown],
        PROVIDER_ERROR,
        [],
    )
    assert not stopped.after_side_effect
    assert transport.closed.is_set()


@pytest.mark.parametrize(
    "pull", [_turn, _turn_pulled_per_task], ids=["one-task", "a-task-per-event"]
)
def test_a_gateway_that_stalls_after_a_piece_ends_the_turn_at_the_deadline(scenario, pull):
    transport = Streamed((an_event_stream(a_chunk("a"), done=False),), hangs=True)

    events, stopped = drive(scenario, transport, pull=pull)

    assert (events, ending(stopped)) == ([AssistantTextEvent(text="a")], LIMIT_EXCEEDED)
    assert transport.closed.is_set()


@pytest.mark.parametrize(
    "first_chunk",
    [
        an_event_stream(a_chunk("a"), done=False),
        an_event_stream(a_chunk("a"), a_chunk("b"), done=False),
    ],
    ids=["next-piece-unread", "next-piece-already-read"],
)
def test_an_app_that_pulls_after_the_deadline_gets_limit_exceeded_and_nothing_more_is_read(
    scenario, first_chunk
):
    transport = Streamed((first_chunk,), hangs=True)
    request = a_request()
    backend = GatewayBackend(scenario.config(), transport=transport)

    async def pull_late():
        events = []
        try:
            async with contextlib.aclosing(backend.run(request, scenario.person()[0])) as stream:
                events.append(await anext(stream))
                await asyncio.sleep(request.max_duration_seconds)
                events.append(await anext(stream))
        except TurnStopped as stopped:
            return events, ending(stopped)
        return events, None

    outcome = in_virtual_time(pull_late())

    assert outcome == ([AssistantTextEvent(text="a")], LIMIT_EXCEEDED)
    assert not transport.started.is_set()
    assert transport.closed.is_set()


@pytest.mark.parametrize("done", [False, True], ids=["alone", "then-done"])
def test_an_error_event_ends_the_turn_and_logs_only_its_type_and_code(scenario, caplog, done):
    caplog.set_level(logging.INFO)
    error = {"message": f"leaks {scenario.key}", "type": "api_error", "code": "500"}
    transport = Streamed((an_event_stream(a_chunk("a"), {"error": error}, done=done),))

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=transport))

    assert (events, ending(stopped), stopped.detail) == (
        [AssistantTextEvent(text="a")],
        UNAVAILABLE,
        GatewayDetail.GATEWAY_FAILED,
    )
    assert "api_error" in caplog.text
    assert "500" in caplog.text
    assert "leaks" not in caplog.text
    assert scenario.key not in caplog.text
    assert transport.closed.is_set()


def test_a_connection_lost_while_the_stream_runs_is_connection_failed(scenario):
    transport = Streamed(
        (an_event_stream(a_chunk("a"), done=False),), breaks_with=httpx2.ReadError("reset")
    )

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=transport))

    assert (events, ending(stopped), stopped.detail) == (
        [AssistantTextEvent(text="a")],
        UNAVAILABLE,
        GatewayDetail.CONNECTION_FAILED,
    )
    assert transport.closed.is_set()


def test_a_tool_rounds_pieces_come_before_its_calls_which_run_after_it_and_echo_whole(scenario):
    first = an_event_stream(
        a_chunk("Looking"),
        a_chunk(tool_calls=[a_fragment(0, call_id="c0", name="lookup", arguments="")]),
        a_chunk(tool_calls=[a_fragment(0, arguments='{"q":')]),
        a_chunk(tool_calls=[a_fragment(0, arguments='"x"}')]),
        a_chunk(finish_reason="tool_calls"),
    )
    gateway = FakeGateway(Reply(body=first), completion("Done."))
    lookup = Lookup()

    events, stopped = drive(scenario, gateway, lookup)

    assert stopped is None
    assert events == [
        AssistantTextEvent(text="Looking"),
        ToolCallEvent(tool_use_id="c0", name="lookup", input={"q": "x"}),
        ToolResultEvent(tool_use_id="c0", content="noted", is_error=False),
        AssistantTextEvent(text="Done."),
        FinalEvent(text="Done."),
    ]
    assert lookup.seen == [{"q": "x"}]
    echoed = json.loads(gateway.requests[1].content)["messages"][-2]
    assert echoed == {
        "role": "assistant",
        "content": "Looking",
        "tool_calls": [
            {
                "id": "c0",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }
        ],
    }


@pytest.mark.parametrize(
    "body",
    [
        an_event_stream(
            a_chunk(tool_calls=[a_fragment(0, call_id="c0", name="lookup", arguments="{}")]),
            a_chunk(finish_reason="tool_calls"),
            done=False,
        ),
        an_event_stream(
            a_chunk(tool_calls=[a_fragment(0, call_id="c0", name="lookup", arguments="{}")]),
            {"error": {"type": "api_error", "code": "500"}},
        ),
    ],
    ids=["cut-before-done", "error-event"],
)
def test_a_call_of_a_round_that_is_not_complete_runs_no_handler(scenario, body):
    lookup = Lookup()

    events, stopped = drive(scenario, Streamed((body,)), lookup)

    assert (events, lookup.seen) == ([], [])
    assert not stopped.after_side_effect
    assert stopped.kind is TurnStopKind.FAILED


def test_a_round_past_the_request_bound_runs_no_handler(scenario):
    first = a_chunk(tool_calls=[a_fragment(0, call_id="c0", name="lookup", arguments='{"q":"')])
    pieces = [
        a_chunk(tool_calls=[a_fragment(0, arguments="x" * MAX_EVENT_BYTES)]) for _ in range(5)
    ]
    last = a_chunk(tool_calls=[a_fragment(0, arguments='"}')])
    body = an_event_stream(first, *pieces, last, a_chunk(finish_reason="tool_calls"))
    lookup = Lookup()

    events, stopped = drive(scenario, Streamed((body,)), lookup)

    assert (events, ending(stopped), lookup.seen) == ([], PROVIDER_ERROR, [])


@pytest.mark.parametrize(
    "body",
    [
        an_event_stream(a_chunk("a"), "{not json marker-in-the-wire"),
        an_event_stream(a_chunk("a"), '{"choices": ' + "[" * 200_000 + " marker-in-the-wire"),
        b"data: \xff marker-in-the-wire\n\n",
        an_event_stream(a_chunk("a"), {"error": {"message": "marker-in-the-wire"}}),
        an_event_stream(a_chunk("\ud800"), STOPPED),
    ],
    ids=["no-json", "nested", "no-utf8", "error-event", "surrogate"],
)
def test_a_stopped_turn_holds_no_text_from_the_wire(scenario, caplog, body):
    caplog.set_level(logging.DEBUG)

    _, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=Streamed((body,))))

    assert (stopped.__context__, stopped.__cause__) == (None, None)
    assert "marker-in-the-wire" not in f"{stopped!r}{stopped}"
    assert "marker-in-the-wire" not in caplog.text
