"""A run's tool set: opened after discovery and closed in five bounded, ordered steps.

Every row drives ``RunTools`` over the real relay on sockets and the real ``open_app_tools``
against ``McpAppFake`` over TLS, on a ``SteppedLoop`` whose ``StepClock`` is the run's clock.
A row runs its closer as its own task under a watchdog that never cancels it, so a close
that waits for a deadline or for the peer fails the row instead of hanging it.
"""

import asyncio
import gc
import threading
from collections.abc import Mapping
from contextlib import AsyncExitStack
from enum import Enum, auto
from typing import Any, cast

import anyio
import httpcore._backends.anyio as httpcore_anyio
import httpx
import pytest
from tests.support.bounds import within_the_bound
from tests.support.mcp_app_fake import (
    FakeResponse,
    Responder,
)
from tests.support.relay_support import (
    RUN_ID,
    RawClient,
    SuppressingOwner,
    json_rpc,
    port_of,
    post,
    post_message,
    yield_until,
)
from tests.support.run_tools_support import (
    CALLS_EVENT,
    CAPABILITY,
    CLIENT_EVENT,
    SUPPORTED,
    TOKEN,
    Cancellation,
    Scene,
    Stall,
    a_loss,
    call_result,
    cli_call,
    door_of,
    ending_of,
    failing_after,
    failing_transport_close,
    finished,
    held_before_the_head,
    injected_reap,
    put_a_forwarded_call_on_the_barrier,
    reply_of,
    run_scene,
    start_close,
    task_names,
    through_parks,
)
from tests.support.service_support import (
    SteppedLoop,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
)

from agent_runtime_service.app_tools import open_app_tools
from agent_runtime_service.run_tools import (
    UPSTREAM_ABORT_SECONDS,
    UPSTREAM_CLOSE_SECONDS,
    RunTools,
)
from agent_runtime_service.tool_relay import (
    RELAY_CLOSE_SECONDS,
    Relay,
    ToolCallBarrier,
)

RELAY_EVENT = f"Closing the relay of run {RUN_ID} failed"
HEAD_BYTES = 40
REFUSAL = {"code": -32602, "message": "Invalid params"}
CLEAN_CLOSE_SECONDS = 0.0
PARKS_OF_EVERY_STEP = (
    UPSTREAM_CLOSE_SECONDS,
    UPSTREAM_CLOSE_SECONDS + UPSTREAM_ABORT_SECONDS,
    UPSTREAM_CLOSE_SECONDS + UPSTREAM_ABORT_SECONDS + RELAY_CLOSE_SECONDS,
)


def refused(request_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": REFUSAL}


def held_after_the_head(_gate: threading.Event) -> Mapping[str, Responder]:
    def respond(request: Any) -> FakeResponse:
        answer = call_result(request)
        return FakeResponse(body=answer.body[:HEAD_BYTES], held_back=answer.body[HEAD_BYTES:])

    return {"tools/call": respond}


def recording_client_exits(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    upstream_tasks_at_exit: list[list[str]] = []
    original = httpx.AsyncClient.aclose

    async def recording(client: httpx.AsyncClient) -> None:
        upstream_tasks_at_exit.append(
            [name for name in task_names(f"upstream:{RUN_ID}:") if not name.endswith("-deadline")]
        )
        await original(client)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", recording)
    return upstream_tasks_at_exit


async def forwarded_from_the_cli(scene: Scene, tools: RunTools) -> tuple[RawClient, RawClient]:
    port = port_of(door_of(tools))
    first, idle = await RawClient.connect(port), await RawClient.connect(port)
    await first.send(cli_call(tools, 1))
    event = await within_the_bound(tools.calls.next_event())
    tools.calls.accept(event)
    await yield_until(lambda: scene.app_calls() != [])
    return first, idle


class Stalls(Enum):
    FORWARD_AND_SETTLE = auto()
    CLIENT = auto()
    EVERY_STEP = auto()


PARKS = {
    Stalls.FORWARD_AND_SETTLE: (UPSTREAM_CLOSE_SECONDS,),
    Stalls.CLIENT: (UPSTREAM_ABORT_SECONDS,),
    Stalls.EVERY_STEP: PARKS_OF_EVERY_STEP,
}
STALLED_LINES = {
    Stalls.FORWARD_AND_SETTLE: [(CALLS_EVENT, "UpstreamCleanupIncomplete")],
    Stalls.CLIENT: [(CLIENT_EVENT, "UpstreamCleanupIncomplete")],
    Stalls.EVERY_STEP: [
        (CALLS_EVENT, "UpstreamCleanupIncomplete"),
        (CLIENT_EVENT, "UpstreamCleanupIncomplete"),
        (RELAY_EVENT, "RelayCleanupIncomplete"),
    ],
}


async def stalled_tools(scene: Scene, plan: Stalls) -> RunTools:
    if plan is not Stalls.EVERY_STEP:
        tools = await scene.open()
        if plan is Stalls.FORWARD_AND_SETTLE:
            scene.stall(httpcore_anyio.AnyIOStream, "aclose")
            scene.stall(ToolCallBarrier, "settle")
            await put_a_forwarded_call_on_the_barrier(scene, tools.calls)
        else:
            scene.stall(httpcore_anyio.AnyIOStream, "aclose")
        return tools
    upstream = AsyncExitStack()
    session = await upstream.enter_async_context(
        open_app_tools(scene.fake.origin, CAPABILITY, scene.context)
    )
    barrier = ToolCallBarrier(RUN_ID, session, CAPABILITY)
    owner = SuppressingOwner()
    scene.on_release(owner.release)
    relay = await Relay.start(RUN_ID, owner, scene.clock)
    holding = await RawClient.connect(port_of(relay))
    await holding.send(post(relay))
    await owner.wait_entered()
    scene.stall(httpcore_anyio.AnyIOStream, "aclose")
    scene.stall(httpx.AsyncHTTPTransport, "aclose")
    scene.stall(ToolCallBarrier, "settle")
    await put_a_forwarded_call_on_the_barrier(scene, barrier)
    return RunTools(RUN_ID, scene.clock, owner, barrier, upstream, relay=relay)


class Target:
    async def call(self) -> str:
        return "ran"


@pytest.mark.parametrize(
    ("method", "listed"),
    [pytest.param("tools/list", True, id="listing"), pytest.param("ping", False, id="ping")],
)
def test_the_tools_tell_whether_their_relay_answered_a_tool_listing(
    authority, monkeypatch, caplog, method, listed
):
    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        before = tools.listed
        client = await RawClient.connect(port_of(door_of(tools)))
        await client.send(post_message(door_of(tools), json_rpc(method, 1), SUPPORTED))
        reply = await reply_of(client)

        assert (before, "result" in reply, tools.listed) == (False, True, listed)
        assert await ending_of(start_close(tools, injected_reap())) is None
        await scene.release_and_drain()

    run_scene(scenario, authority, monkeypatch, caplog)


def test_a_stall_released_before_its_first_call_lets_that_call_through(monkeypatch):
    stall = Stall(monkeypatch, Target, "call")
    stall.release()

    async def first_call() -> str:
        call = asyncio.ensure_future(Target().call())
        try:
            await finished(call)
        finally:
            stall.release()
        return call.result()

    assert asyncio.run(first_call()) == "ran"
    assert stall.original_ran
    assert not stall.pending


@pytest.mark.parametrize("app_holds", [held_before_the_head, held_after_the_head])
def test_the_close_stops_before_the_reap_and_needs_neither_clock_time_nor_the_peer(
    authority, monkeypatch, caplog, app_holds
):
    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        client_exits = recording_client_exits(scene.monkeypatch)
        first, idle = await forwarded_from_the_cli(scene, tools)
        port = port_of(door_of(tools))
        seen_at_reap: list[list[str]] = []

        async def at_the_reap() -> None:
            with pytest.raises(ConnectionRefusedError):
                await RawClient.connect(port)
            await idle.send(cli_call(tools, 2))
            assert await reply_of(idle) == refused(2)
            assert await reply_of(first) == refused(1)
            seen_at_reap.append(task_names(f"upstream:{RUN_ID}:") + task_names(f"relay:{RUN_ID}:"))

        closer = start_close(tools, injected_reap(on_entry=at_the_reap))
        assert await ending_of(closer) is None

        (running_at_reap,) = seen_at_reap
        assert set(running_at_reap) >= {
            f"upstream:{RUN_ID}:call-1",
            f"relay:{RUN_ID}:conn-1",
            f"relay:{RUN_ID}:conn-2",
        }
        assert client_exits == [[f"upstream:{RUN_ID}:client"]]
        assert scene.clock.now == CLEAN_CLOSE_SECONDS
        assert not scene.app_gate.is_set()
        assert not scene.fake.tail_released.is_set()
        await scene.assert_nothing_left()
        await scene.release_and_drain()
        assert len(scene.app_calls()) == 1
        await scene.app_saw_every_connection_end()
        assert scene.logged() == []

    run_scene(scenario, authority, monkeypatch, caplog, app_holds)


@pytest.mark.parametrize("weakly", [False, True], ids=["gate-kept", "gate-in-the-frame-only"])
@pytest.mark.parametrize("plan", list(Stalls))
def test_a_stalled_cleanup_ends_its_step_at_its_own_deadline_and_still_closes_after_it(
    authority, monkeypatch, caplog, plan, weakly
):
    async def scenario(scene: Scene) -> None:
        scene.stalls_held_weakly = weakly
        tools = await stalled_tools(scene, plan)
        closer = start_close(tools, injected_reap())

        await through_parks(scene, PARKS[plan])

        assert await ending_of(closer) is None
        assert scene.clock.now == PARKS[plan][-1]
        assert [stall.pending for stall in scene.stalls] == [True] * len(scene.stalls)
        gc.collect()
        await scene.release_and_drain()
        assert [stall.cancelling for stall in scene.stalls] == [1] * len(scene.stalls)
        assert all(stall.original_ran for stall in scene.stalls)
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == STALLED_LINES[plan]
        assert "Task was destroyed" not in caplog.text

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)


class Interrupt(BaseException):
    pass


@pytest.mark.parametrize("failure", [a_loss, Interrupt], ids=["an Exception", "a BaseException"])
def test_a_reap_failure_of_any_class_is_raised_after_the_last_step_when_the_steps_are_clean(
    authority, monkeypatch, caplog, failure
):
    loss = failure()

    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        await forwarded_from_the_cli(scene, tools)
        closer = start_close(tools, injected_reap(raises=loss))

        assert await ending_of(closer) is loss
        assert scene.clock.now == CLEAN_CLOSE_SECONDS
        await scene.assert_nothing_left()
        await scene.release_and_drain()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == []

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)


def test_a_containment_loss_is_raised_after_the_last_step_when_every_step_stalls(
    authority, monkeypatch, caplog
):
    loss = a_loss()

    async def scenario(scene: Scene) -> None:
        tools = await stalled_tools(scene, Stalls.EVERY_STEP)
        closer = start_close(tools, injected_reap(raises=loss))

        await through_parks(scene, PARKS[Stalls.EVERY_STEP])

        assert await ending_of(closer) is loss
        assert scene.logged() == STALLED_LINES[Stalls.EVERY_STEP]

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)


@pytest.mark.parametrize("cancellation", list(Cancellation))
@pytest.mark.parametrize("raises_loss", [False, True])
def test_a_callers_cancellation_never_cuts_the_close_short_and_is_delivered_after_it(
    authority, monkeypatch, caplog, cancellation, raises_loss
):
    loss = a_loss()

    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        await forwarded_from_the_cli(scene, tools)
        in_the_reap, reap_gate = asyncio.Event(), asyncio.Event()
        reached_after_close = asyncio.Event()
        scope = anyio.CancelScope()

        async def entered() -> None:
            in_the_reap.set()

        reap = injected_reap(gate=reap_gate, raises=loss if raises_loss else None, on_entry=entered)

        async def closing_in_scope() -> None:
            with scope:
                await tools.close(reap)
                reached_after_close.set()

        closer = asyncio.get_running_loop().create_task(
            closing_in_scope() if cancellation is Cancellation.LEVEL else tools.close(reap),
            name="test:closer",
        )
        await within_the_bound(in_the_reap.wait())
        if cancellation is Cancellation.LEVEL:
            scope.cancel()
        else:
            closer.cancel()
        await cast(SteppedLoop, asyncio.get_running_loop()).settled()
        assert not closer.done()
        reap_gate.set()

        ending = await ending_of(closer)

        if raises_loss:
            assert ending is loss
            assert scope.cancelled_caught is False
            if cancellation is Cancellation.DIRECT:
                assert closer.cancelling() == 1
        elif cancellation is Cancellation.DIRECT:
            assert isinstance(ending, asyncio.CancelledError)
        else:
            assert ending is None
            assert scope.cancelled_caught
            assert not reached_after_close.is_set()
        await scene.assert_nothing_left()
        await scene.release_and_drain()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == []

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)


CLOSE_STEPS = {
    "the settle": (ToolCallBarrier, "settle", CALLS_EVENT),
    "the client's exit": (httpx.AsyncClient, "aclose", CLIENT_EVENT),
    "the relay's close": (Relay, "close", RELAY_EVENT),
}


@pytest.mark.parametrize(
    "reap_loss", [None, a_loss, Interrupt], ids=["no loss", "a loss", "an interrupt"]
)
@pytest.mark.parametrize("ends_with", [asyncio.CancelledError, Interrupt])
@pytest.mark.parametrize("step", CLOSE_STEPS.values(), ids=CLOSE_STEPS.keys())
def test_a_step_ending_with_any_class_is_logged_once_and_the_close_goes_on(
    authority, monkeypatch, caplog, step, ends_with, reap_loss
):
    owner, name, event = step
    loss = reap_loss() if reap_loss else None

    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        failing_after(scene.monkeypatch, owner, name, ends_with)
        closer = start_close(tools, injected_reap(raises=loss))

        assert await ending_of(closer) is loss
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == [(event, ends_with.__name__)]

    run_scene(scenario, authority, monkeypatch, caplog)


def test_a_failing_step_is_logged_by_class_and_leaks_no_token(authority, monkeypatch, caplog):
    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        failing_transport_close(scene.monkeypatch)
        closer = start_close(tools, injected_reap())

        assert await ending_of(closer) is None
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == [(CLIENT_EVENT, "UpstreamCallLost")]
        signature = TOKEN.rsplit(".", 1)[-1]
        assert [secret in caplog.text for secret in (TOKEN, signature)] == [False, False]

    run_scene(scenario, authority, monkeypatch, caplog)
