"""``GatewayBackend`` running an app's hand tools: the rows of the tool-loop matrix (M-L2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_runtime_client.agents import TurnRequest
from agent_runtime_client.backend import TurnPerson, TurnStopKind, TurnStopped
from agent_runtime_client.backend.fake_gateway import (
    FakeGateway,
    Reply,
    completion,
    tool_call,
    tool_round,
)
from agent_runtime_client.backend.gateway import GatewayBackend
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.tools import (
    HandTool,
    JsonObject,
    Principal,
    ToolDeclaration,
    ToolEffect,
    ToolOutcome,
)
from agent_runtime_client.turn import (
    MAX_EVENT_BYTES,
    FailureCode,
    RejectionCode,
    Turn,
    TurnMessage,
    TurnRole,
)
from agent_runtime_client.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)

PROVIDER_ERROR = (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)
LIMIT_EXCEEDED = (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
BUSY = (TurnStopKind.REFUSED, RejectionCode.BUSY)


@dataclass
class Shop:
    """The app side of a turn: its hand tools, what they were called with, and the gateway."""

    directory: Path
    replies: tuple[Reply, ...] = ()
    seen: list[tuple[str, Principal, JsonObject]] = field(default_factory=list)
    tools: list[HandTool] = field(default_factory=list)

    def add_tool(
        self,
        name: str,
        *,
        effect: ToolEffect = ToolEffect.READ_ONLY,
        answer: Callable[[], Awaitable[ToolOutcome]] | None = None,
    ) -> HandTool:
        async def handler(principal: Principal, arguments: JsonObject) -> ToolOutcome:
            self.seen.append((name, principal, arguments))
            return await (answer or _noted)()

        tool = HandTool(
            ToolDeclaration(name=name, description=f"Use {name}.", parameters={}, effect=effect),
            handler,
        )
        self.tools.append(tool)
        return tool

    def run(
        self,
        *replies: Reply,
        max_tool_calls: int = 5,
        selected: tuple[str, ...] | None = None,
        max_duration_seconds: float = 30,
    ) -> tuple[list, TurnStopped | None, FakeGateway]:
        gateway = FakeGateway(*replies)
        chosen = [t for t in self.tools if selected is None or t.declaration.name in selected]
        request = a_request(chosen, max_tool_calls, max_duration_seconds)
        backend = GatewayBackend(self.config(), tools=self.tools, transport=gateway)
        events, stopped = asyncio.run(_turn(backend, request))
        return events, stopped, gateway

    def config(self) -> AgentConfig:
        key_file = self.directory / "key"
        key_file.write_text("stand-in-key-0001\n")
        gateway = {"origin": "http://gateway.test", "key_file": str(key_file)}
        return AgentConfig.model_validate(
            {"backend": "litellm", "gateway": gateway, "api_group": "/apps/example"}
        )


async def _noted() -> ToolOutcome:
    return ToolOutcome("noted", is_error=False)


def a_request(
    tools: list[HandTool], max_tool_calls: int, max_duration_seconds: float = 30
) -> TurnRequest:
    request = TurnRequest(
        agent="helper",
        turn=Turn(system="Answer briefly.", messages=[TurnMessage(role=TurnRole.USER, text="Hi")]),
        roles=["admin", "user"],
        tools=tuple(tool.declaration for tool in tools),
        model="text/standin",
        max_duration_seconds=30,
        max_tool_calls=max_tool_calls if tools else None,
    )
    return request.model_copy(update={"max_duration_seconds": max_duration_seconds})


def a_person() -> TurnPerson:
    async def groups() -> frozenset[str]:
        return frozenset({"/apps/example"})

    return TurnPerson(
        principal=Principal(iss="https://issuer.example", sub="s-1"), role="user", groups=groups
    )


async def _turn(backend: GatewayBackend, request: TurnRequest):
    events = []
    try:
        async with contextlib.aclosing(backend.run(request, a_person())) as stream:
            async for event in stream:
                events.append(event)
    except TurnStopped as stopped:
        return events, stopped
    return events, None


def ending(stopped: TurnStopped | None):
    return None if stopped is None else (stopped.kind, stopped.code)


def sent(gateway: FakeGateway, index: int) -> dict:
    return json.loads(gateway.requests[index].content)


@pytest.fixture
def shop(tmp_path: Path) -> Shop:
    return Shop(tmp_path)


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
    assert shop.seen[0][1] == Principal(iss="https://issuer.example", sub="s-1")
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


@pytest.mark.parametrize(
    "bad_round",
    [
        tool_round(tool_call("c1", "lookup", {}), tool_call("c2", "unoffered", {})),
        tool_round(tool_call("c1", "lookup", "{not json")),
        tool_round(tool_call("c1", "lookup", "[]")),
        tool_round(tool_call("c1", "lookup", {}), tool_call("c1", "lookup", {})),
        tool_round(),
        completion("never", finish_reason="length"),
    ],
    ids=["unoffered", "not-json", "not-an-object", "duplicate-id", "no-calls", "other-ending"],
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
    assert sent(gateway, 1)["messages"][-1]["content"] == "no such note"
    assert events[-1] == FinalEvent(text="Sorry.")


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
    assert "secret-note-title" not in events[1].content
    assert "secret-note-title" not in gateway.requests[1].content.decode()
    assert "KeyError" in caplog.text
    assert "secret-note-title" not in caplog.text
    assert "Traceback" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_each_call_logs_its_name_id_and_error_flag_never_arguments_or_content(shop, caplog):
    async def answer() -> ToolOutcome:
        return ToolOutcome("content-canary", is_error=True)

    shop.add_tool("lookup", answer=answer)
    caplog.set_level(logging.DEBUG)

    shop.run(tool_round(tool_call("c1", "lookup", {"k": "argument-canary"})), completion("ok"))

    calls = [r for r in caplog.records if r.levelno == logging.INFO and "lookup" in r.getMessage()]
    assert len(calls) == 1
    assert "c1" in calls[0].getMessage()
    assert "True" in calls[0].getMessage()
    assert "argument-canary" not in caplog.text
    assert "content-canary" not in caplog.text


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
def test_a_deadline_during_a_handler_cancels_it_and_keeps_the_side_effect_state(shop, effect):
    cancelled = []

    async def stuck() -> ToolOutcome:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return ToolOutcome("never", is_error=False)

    shop.add_tool("lookup", effect=effect, answer=stuck)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {})), completion("never"), max_duration_seconds=0.05
    )

    assert ending(stopped) == LIMIT_EXCEEDED
    assert stopped.after_side_effect is (effect is ToolEffect.SIDE_EFFECTING)
    assert cancelled == [True]
    assert [type(event) for event in events] == [ToolCallEvent]
    assert len(gateway.requests) == 1


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


def test_cancelling_between_a_call_and_its_result_reaches_the_caller_unchanged(shop):
    started = asyncio.Event()

    async def stuck() -> ToolOutcome:
        started.set()
        await asyncio.Event().wait()
        return ToolOutcome("never", is_error=False)

    shop.add_tool("lookup", effect=ToolEffect.SIDE_EFFECTING, answer=stuck)
    gateway = FakeGateway(tool_round(tool_call("c1", "lookup", {})), completion("never"))
    backend = GatewayBackend(shop.config(), tools=shop.tools, transport=gateway)
    events: list = []

    async def consume() -> None:
        async with contextlib.aclosing(backend.run(a_request(shop.tools, 3), a_person())) as stream:
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


@pytest.mark.parametrize(
    ("extra_bytes", "reaches_the_model"), [(0, True), (1, False)], ids=["at-bound", "one-over"]
)
def test_a_tool_result_over_the_event_bound_is_replaced_and_the_side_effect_stands(
    shop, extra_bytes, reaches_the_model
):
    async def big() -> ToolOutcome:
        return ToolOutcome("a" * (MAX_EVENT_BYTES + extra_bytes), is_error=False)

    shop.add_tool("write", effect=ToolEffect.SIDE_EFFECTING, answer=big)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "write", {})), Reply(429), max_tool_calls=3
    )

    result = events[1]
    assert (result.content == "a" * MAX_EVENT_BYTES) is reaches_the_model
    assert result.is_error is (not reaches_the_model)
    assert sent(gateway, 1)["messages"][-1]["content"] == result.content
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


def test_a_selected_tool_without_a_handler_raises_before_any_request(shop):
    shop.add_tool("lookup")
    gateway = FakeGateway(completion("never"))
    request = a_request(shop.tools, 3)
    backend = GatewayBackend(shop.config(), tools=(), transport=gateway)

    with pytest.raises(ValueError, match="lookup"):
        asyncio.run(_turn(backend, request))

    assert gateway.requests == []


def test_the_request_carries_only_model_messages_user_and_tools_without_the_effect(shop):
    shop.add_tool("lookup", effect=ToolEffect.READ_ONLY)

    _, _, gateway = shop.run(completion("ok"))

    body = sent(gateway, 0)
    assert set(body) == {"model", "messages", "user", "tools"}
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "lookup", "description": "Use lookup.", "parameters": {}},
        }
    ]
