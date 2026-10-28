"""Opening a tool session and attaching its listener share one bounded cleanup lifetime."""

import asyncio
import errno
import inspect
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import httpcore._backends.anyio as httpcore_anyio
import httpx
import pytest
from tests.support.bounds import TEST_WAIT_SECONDS, within_the_bound
from tests.support.mcp_app_fake import FakeResponse, Responder, rpc_result, tool_listing
from tests.support.relay_support import (
    RUN_ID,
    RawClient,
    json_rpc,
    parked_at,
    port_of,
    post_message,
    yield_until,
)
from tests.support.service_support import fake_app_authority  # noqa: F401 - a fixture, requested by name
from tests.test_service_run_tools import (
    CALLS_EVENT,
    CAPABILITY,
    CLIENT_EVENT,
    OFFERED,
    SUPPORTED,
    Cancellation,
    Scene,
    a_loss,
    cli_call,
    door_of,
    ending_of,
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

from agent_runtime_client.mcp_wire import CallToolParams, JsonRpcError, JsonRpcErrorCode
from agent_runtime_service.app_tools import AppToolsSession, UpstreamRefused
from agent_runtime_service.run_tools import UPSTREAM_ABORT_SECONDS, UPSTREAM_CLOSE_SECONDS, RunTools
from agent_runtime_service.tool_relay import (
    McpRelayOwner,
    RepeatedToolOffer,
    ToolCallBarrier,
    UnofferedTool,
)


def listing_held(gate: threading.Event) -> Mapping[str, Responder]:
    def respond(request: Any) -> FakeResponse:
        gate.wait(TEST_WAIT_SECONDS)
        return rpc_result(request, tool_listing(OFFERED))

    return {"tools/list": respond}


def test_open_builds_the_tool_set_after_discovery(authority, monkeypatch, caplog):
    async def scenario(scene: Scene) -> None:
        opening = asyncio.get_running_loop().create_task(scene.open(), name="test:opener")
        await yield_until(lambda: any(r.rpc_method == "tools/list" for r in scene.fake.requests))
        assert scene.listeners.servers == []
        scene.app_gate.set()

        await finished(opening)
        tools = opening.result()

        assert len(scene.listeners.serving) == 1
        client = await RawClient.connect(port_of(door_of(tools)))
        await client.send(post_message(door_of(tools), json_rpc("tools/list", 1), SUPPORTED))
        listed = await reply_of(client)
        assert [tool["name"] for tool in listed["result"]["tools"]] == ["edit_song"]
        await client.send(cli_call(tools, 2))
        assert (await within_the_bound(tools.calls.next_event())).name == "edit_song"
        closer = start_close(tools, injected_reap())
        assert await ending_of(closer) is None
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()

    run_scene(scenario, authority, monkeypatch, caplog, listing_held)


def remembering_raised(
    monkeypatch: pytest.MonkeyPatch, owner: object, name: str
) -> list[BaseException]:
    raised: list[BaseException] = []
    original = getattr(owner, name)

    async def remembering_async(*args: Any, **kwargs: Any) -> Any:
        try:
            return await original(*args, **kwargs)
        except BaseException as failure:
            raised.append(failure)
            raise

    def remembering(*args: Any, **kwargs: Any) -> Any:
        try:
            return original(*args, **kwargs)
        except BaseException as failure:
            raised.append(failure)
            raise

    monkeypatch.setattr(
        owner, name, remembering_async if inspect.iscoroutinefunction(original) else remembering
    )
    return raised


@dataclass(frozen=True)
class FailedOpen:
    expected: type[BaseException]
    lines: list[tuple[str, str]]
    refuses_listing: bool = False
    listed: tuple[str, ...] | None = None
    fails_transport_close: bool = False
    stalls_cleanup: bool = False
    raised_by: tuple[type, str] | None = None


FAILED_OPENS = {
    "the listing is refused": FailedOpen(
        UpstreamRefused, [], refuses_listing=True, raised_by=(AppToolsSession, "list_tools")
    ),
    "an allowed tool is offered twice and the client's exit fails": FailedOpen(
        RepeatedToolOffer,
        [(CLIENT_EVENT, "UpstreamCallLost")],
        listed=("edit_song", "edit_song"),
        fails_transport_close=True,
        raised_by=(McpRelayOwner, "__init__"),
    ),
    "a signed tool is missing from the listing": FailedOpen(
        UnofferedTool, [], listed=("delete_song",), raised_by=(McpRelayOwner, "__init__")
    ),
    "the relay cannot listen and the client's cleanup stalls": FailedOpen(
        OSError, [(CLIENT_EVENT, "UpstreamCleanupIncomplete")], stalls_cleanup=True
    ),
}


@pytest.mark.parametrize("case", FAILED_OPENS.values(), ids=FAILED_OPENS.keys())
def test_a_failed_open_leaves_the_client_and_keeps_its_own_failure(
    authority, monkeypatch, caplog, case
):
    def refusing_the_listing(_gate: threading.Event) -> Mapping[str, Responder]:
        refusal = FakeResponse(status=500, content_type=None)
        return {"tools/list": lambda _request: refusal} if case.refuses_listing else {}

    async def scenario(scene: Scene) -> None:
        address_in_use = OSError(errno.EADDRINUSE, "in use")
        raised = (
            remembering_raised(scene.monkeypatch, *case.raised_by)
            if case.raised_by
            else [address_in_use]
        )
        if case.stalls_cleanup:
            scene.listeners.failure = address_in_use
            stall = scene.stall(httpcore_anyio.AnyIOStream, "aclose")
        if case.fails_transport_close:
            failing_transport_close(scene.monkeypatch)
        opening = asyncio.get_running_loop().create_task(scene.open(), name="test:opener")

        if case.stalls_cleanup:
            await through_parks(scene, (UPSTREAM_ABORT_SECONDS,))
        ending = await ending_of(opening)

        assert isinstance(ending, case.expected)
        assert ending is raised[0]
        if case.stalls_cleanup:
            assert stall.pending
            await scene.release_and_drain()
            assert stall.original_ran
        assert scene.listeners.serving == []
        await scene.release_and_drain()
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == case.lines
        assert "edit_song" not in caplog.text

    pages = [tool_listing(list(case.listed))] if case.listed else None
    run_scene(scenario, authority, monkeypatch, caplog, refusing_the_listing, pages)


OPENER_CANCELS = {
    "directly once": (Cancellation.DIRECT, 1),
    "directly again after the client step parked": (Cancellation.DIRECT, 2),
    "by level": (Cancellation.LEVEL, 1),
}


@pytest.mark.parametrize("phase", ["discovery", "attachment"])
@pytest.mark.parametrize("cancellation, times", OPENER_CANCELS.values(), ids=OPENER_CANCELS.keys())
def test_a_failed_open_outlasts_cancellations_during_its_client_cleanup_and_keeps_its_failure(
    authority, monkeypatch, caplog, cancellation, times, phase
):
    def refusing_the_listing(_gate: threading.Event) -> Mapping[str, Responder]:
        refusal = FakeResponse(status=500, content_type=None)
        return {"tools/list": lambda _request: refusal} if phase == "discovery" else {}

    async def scenario(scene: Scene) -> None:
        source = (
            (AppToolsSession, "list_tools") if phase == "discovery" else (asyncio, "start_server")
        )
        if phase == "attachment":
            scene.listeners.failure = OSError(errno.EADDRINUSE, "in use")
        original_failures = remembering_raised(scene.monkeypatch, *source)
        stall = scene.stall(httpx.AsyncHTTPTransport, "aclose")
        scope = anyio.CancelScope()

        async def opening_in_scope() -> RunTools | None:
            with scope:
                return await scene.open()
            return None

        opening = asyncio.get_running_loop().create_task(
            opening_in_scope() if cancellation is Cancellation.LEVEL else scene.open(),
            name="test:opener",
        )
        await yield_until(lambda: stall.pending)
        if cancellation is Cancellation.LEVEL:
            scope.cancel()
        else:
            opening.cancel()
        if times == 2:
            await parked_at(scene.clock, UPSTREAM_ABORT_SECONDS)
            opening.cancel()
        await through_parks(scene, (UPSTREAM_ABORT_SECONDS,))
        ending = await ending_of(opening)

        assert ending is original_failures[0]
        assert scene.clock.now == UPSTREAM_ABORT_SECONDS
        assert stall.pending
        await scene.release_and_drain()
        assert stall.cancelling == 1
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == [(CLIENT_EVENT, "UpstreamCleanupIncomplete")]

    run_scene(scenario, authority, monkeypatch, caplog, refusing_the_listing)


def hold_listener_start(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    attaching, gate = asyncio.Event(), asyncio.Event()
    original = asyncio.start_server

    async def held(*args: Any, **kwargs: Any) -> asyncio.Server:
        attaching.set()
        await gate.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "start_server", held)
    return attaching


@pytest.mark.parametrize("phase", ["discovery", "attachment"])
@pytest.mark.parametrize("cancellation", list(Cancellation))
def test_a_cancelled_open_parks_its_stalled_client_cleanup_and_keeps_the_cancellation(
    authority, monkeypatch, caplog, cancellation, phase
):
    async def scenario(scene: Scene) -> None:
        stall = scene.stall(httpx.AsyncHTTPTransport, "aclose")
        scope = anyio.CancelScope()
        attaching = hold_listener_start(scene.monkeypatch) if phase == "attachment" else None
        source = (
            (AppToolsSession, "list_tools") if phase == "discovery" else (asyncio, "start_server")
        )
        original_failures = remembering_raised(scene.monkeypatch, *source)
        propagated = remembering_raised(scene.monkeypatch, scene, "open")

        async def opening_in_scope() -> RunTools | None:
            with scope:
                return await scene.open()
            return None

        opening = asyncio.get_running_loop().create_task(
            opening_in_scope() if cancellation is Cancellation.LEVEL else scene.open(),
            name="test:opener",
        )
        if attaching is not None:
            await within_the_bound(attaching.wait())
        else:
            await yield_until(
                lambda: any(r.rpc_method == "tools/list" for r in scene.fake.requests)
            )
        if cancellation is Cancellation.LEVEL:
            scope.cancel()
        else:
            opening.cancel()

        await through_parks(scene, (UPSTREAM_ABORT_SECONDS,))
        ending = await ending_of(opening)

        assert len(propagated) == 1
        assert propagated[0] is original_failures[0]
        if cancellation is Cancellation.LEVEL:
            assert ending is None
            assert scope.cancelled_caught
        else:
            assert isinstance(ending, asyncio.CancelledError)
        assert scene.clock.now == UPSTREAM_ABORT_SECONDS
        assert stall.pending
        assert scene.listeners.serving == []
        await scene.release_and_drain()
        assert stall.original_ran
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == [(CLIENT_EVENT, "UpstreamCleanupIncomplete")]

    holds = listing_held if phase == "discovery" else lambda _gate: {}
    run_scene(scenario, authority, monkeypatch, caplog, holds)


@pytest.mark.parametrize("stalled", [False, True], ids=["clean", "stalled"])
@pytest.mark.parametrize("raises_loss", [False, True], ids=["reaped", "reap-failed"])
def test_a_listener_free_session_stops_calls_and_closes_with_the_same_bounds(
    authority, monkeypatch, caplog, stalled, raises_loss
):
    loss = a_loss() if raises_loss else None

    async def scenario(scene: Scene) -> None:
        tools = await RunTools.open_session(
            RUN_ID, scene.fake.origin, CAPABILITY, scene.context, scene.clock
        )
        assert scene.listeners.servers == []
        assert [request.rpc_method for request in scene.fake.requests].count("tools/list") == 1
        if stalled:
            scene.stall(httpcore_anyio.AnyIOStream, "aclose")
            scene.stall(ToolCallBarrier, "settle")
            scene.stall(httpx.AsyncHTTPTransport, "aclose")
        await put_a_forwarded_call_on_the_barrier(scene, tools.calls)
        seen_at_reap: list[int] = []

        async def at_the_reap() -> None:
            reply = await tools.calls.call(2, CallToolParams(name="edit_song", arguments={}))
            assert isinstance(reply, JsonRpcError)
            assert reply.id == 2
            assert reply.error.code == JsonRpcErrorCode.INVALID_PARAMS
            seen_at_reap.append(len(scene.app_calls()))

        closer = start_close(tools, injected_reap(raises=loss, on_entry=at_the_reap))
        if stalled:
            await through_parks(
                scene, (UPSTREAM_CLOSE_SECONDS, UPSTREAM_CLOSE_SECONDS + UPSTREAM_ABORT_SECONDS)
            )
        assert await ending_of(closer) is loss
        assert seen_at_reap == [1]
        assert scene.clock.now == (2.5 if stalled else 0.0)
        assert [stall.pending for stall in scene.stalls] == [True] * len(scene.stalls)
        await scene.release_and_drain()
        assert all(stall.original_ran for stall in scene.stalls)
        assert [stall.cancelling for stall in scene.stalls] == [1] * len(scene.stalls)
        assert len(scene.app_calls()) == 1
        await scene.assert_nothing_left()
        assert task_names(f"run-tools:{RUN_ID}:") == []
        await scene.app_saw_every_connection_end()
        assert scene.logged() == (
            [
                (CALLS_EVENT, "UpstreamCleanupIncomplete"),
                (CLIENT_EVENT, "UpstreamCleanupIncomplete"),
            ]
            if stalled
            else []
        )

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)
