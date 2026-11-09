"""``GatewayBackend`` reading a round from an event stream: what a turn does with it."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import httpx2
import pytest

from agent_runtime_client.backend import TurnStopKind
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
from tests.support.virtual_time import VirtualTimeLoop

scenario = gateway_turn_support.scenario

PROVIDER_ERROR = (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)
LIMIT_EXCEEDED = (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
STOPPED = a_chunk(finish_reason="stop")


@dataclass
class Lookup:
    """The app's one tool; ``seen`` holds the arguments of every call it ran."""

    seen: list = field(default_factory=list)

    def tool(self) -> HandTool:
        async def handler(principal, arguments):
            self.seen.append(arguments)
            return ToolOutcome("noted", is_error=False)

        declaration = ToolDeclaration(
            name="lookup", description="Look up.", parameters={}, effect=ToolEffect.READ_ONLY
        )
        return HandTool(declaration, handler)


def drive(scenario, transport, lookup: Lookup | None = None):
    lookup = lookup or Lookup()
    tool = lookup.tool()
    request = a_request(tools=(tool.declaration,), max_tool_calls=3)
    backend = GatewayBackend(scenario.config(), tools=[tool], transport=transport)
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        return runner.run(_turn(backend, request, scenario.person()[0]))


def test_the_pieces_of_a_text_round_come_as_one_event_of_the_joined_text(scenario):
    body = an_event_stream(a_chunk("Hel"), a_chunk("lo ä"), a_chunk("!"), STOPPED)
    transport = Streamed((body,))

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=transport))

    assert stopped is None
    assert events == [AssistantTextEvent(text="Hello ä!"), FinalEvent(text="Hello ä!")]


@pytest.mark.parametrize(
    "body",
    [
        an_event_stream(a_chunk("a"), done=False),
        an_event_stream(a_chunk("a")),
        an_event_stream(a_chunk("a"), "{not json", STOPPED),
        an_event_stream(a_chunk("a"), a_chunk(finish_reason="length")),
        an_event_stream(a_chunk("a"), STOPPED, done=False),
    ],
    ids=[
        "cut-after-a-piece",
        "done-without-a-finish-reason",
        "malformed-event-after-a-piece",
        "length",
        "cut-after-the-finish-reason",
    ],
)
def test_a_round_that_is_not_complete_ends_the_turn_and_closes_the_response(scenario, body):
    transport = Streamed((body,))

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=transport))

    assert (events, ending(stopped)) == ([], PROVIDER_ERROR)
    assert transport.closed.is_set()


def test_a_gateway_that_stalls_after_a_piece_ends_the_turn_at_the_deadline(scenario):
    transport = Streamed((an_event_stream(a_chunk("a"), done=False),), hangs=True)

    events, stopped = drive(scenario, transport)

    assert (events, ending(stopped)) == ([], LIMIT_EXCEEDED)
    assert transport.closed.is_set()


@pytest.mark.parametrize("done", [False, True], ids=["alone", "then-done"])
def test_an_error_event_ends_the_turn_and_logs_only_its_type_and_code(scenario, caplog, done):
    caplog.set_level(logging.INFO)
    error = {"message": f"leaks {scenario.key}", "type": "api_error", "code": "500"}
    transport = Streamed((an_event_stream(a_chunk("a"), {"error": error}, done=done),))

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=transport))

    assert (events, ending(stopped), stopped.detail) == (
        [],
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
        [],
        UNAVAILABLE,
        GatewayDetail.CONNECTION_FAILED,
    )
    assert transport.closed.is_set()


def test_calls_streamed_in_fragments_run_after_the_round_and_are_echoed_whole(scenario):
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
