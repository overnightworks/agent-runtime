"""The scene a ``RunTools`` close is driven in."""

import asyncio
import contextlib
import gc
import json
import ssl
import threading
import weakref
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum, auto
from typing import Any, cast

import httpx
import pytest
from tests.support.bounds import TEST_WAIT_SECONDS, within_the_bound
from tests.support.capability_support import canary_token, capability_carrying
from tests.support.mcp_app_fake import (
    CERTIFICATE_HOST,
    FakeResponse,
    McpAppFake,
    Responder,
    ScriptedApp,
    rpc_result,
    serving,
    tool_listing,
)
from tests.support.relay_support import (
    RUN_ID,
    RawClient,
    RelayDoor,
    json_rpc,
    parked_at,
    post_message,
    relay_tasks,
    yield_until,
)
from tests.support.service_support import (
    StepClock,
    SteppedLoop,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
)

from agent_runtime.errors import ContainmentLost, RetainedGroup
from agent_runtime_client.mcp_wire import CallToolParams
from agent_runtime_service.run_tools import (
    RunTools,
)
from agent_runtime_service.tool_relay import (
    ToolCallBarrier,
)

SUPPORTED = "2025-11-25"
TOKEN = canary_token()
CAPABILITY = capability_carrying(TOKEN, tools=("edit_song",))
OFFERED = ["edit_song", "delete_song"]
CALLS_EVENT = f"Closing the upstream calls of run {RUN_ID} failed"
CLIENT_EVENT = f"Leaving the app's client of run {RUN_ID} failed"


def call_result(request: Any) -> FakeResponse:
    return rpc_result(request, {"content": [{"type": "text", "text": "done"}]})


def held_before_the_head(gate: threading.Event) -> Mapping[str, Responder]:
    def respond(request: Any) -> FakeResponse:
        gate.wait(TEST_WAIT_SECONDS)
        return call_result(request)

    return {"tools/call": respond}


def task_names(prefix: str) -> list[str]:
    return sorted(
        name for task in asyncio.all_tasks() if (name := task.get_name()).startswith(prefix)
    )


def injected_reap(
    *,
    gate: asyncio.Event | None = None,
    raises: BaseException | None = None,
    on_entry: Callable[[], Awaitable[None]] | None = None,
) -> Callable[[], Awaitable[None]]:
    async def reap() -> None:
        if on_entry is not None:
            await on_entry()
        if gate is not None:
            await gate.wait()
        if raises is not None:
            raise raises

    return reap


class Stall:
    """Absorbs every cancellation of the target's first call until released, then runs it.

    A weakly held stall keeps its gate reachable only from the stalled coroutine's frame.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, owner: type, name: str, *, weakly: bool = False
    ) -> None:
        self._gate: weakref.ref[asyncio.Event] | None = None
        self._strong_gate: asyncio.Event | None = None
        self.pending = False
        self.original_ran = False
        self.cancelling = 0
        self._claimed = False
        self._released = False
        original = getattr(owner, name)

        async def stalled(*args: Any, **kwargs: Any) -> Any:
            if self._claimed:
                return await original(*args, **kwargs)
            self._claimed = True
            if self._released:
                self.original_ran = True
                return await original(*args, **kwargs)
            gate = asyncio.Event()
            self._gate = weakref.ref(gate)
            self._strong_gate = None if weakly else gate
            self.pending = True
            while not gate.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await gate.wait()
            self.pending = False
            self.cancelling = cast(asyncio.Task[Any], asyncio.current_task()).cancelling()
            self.original_ran = True
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, stalled)

    def release(self) -> None:
        self._released = True
        gate = self._gate() if self._gate is not None else None
        if gate is not None:
            gate.set()


class ListenerRecorder:
    """Wraps ``asyncio.start_server``: records each listener, or fails with ``failure``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.servers: list[asyncio.Server] = []
        self.ports: list[int] = []
        self.failure: OSError | None = None
        original = asyncio.start_server

        async def recording(*args: Any, **kwargs: Any) -> asyncio.Server:
            if self.failure is not None:
                raise self.failure
            server = await original(*args, **kwargs)
            self.servers.append(server)
            self.ports.append(server.sockets[0].getsockname()[1])
            return server

        monkeypatch.setattr(asyncio, "start_server", recording)

    @property
    def serving(self) -> list[asyncio.Server]:
        return [server for server in self.servers if server.is_serving()]


def failing_transport_close(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request(
        "POST", "https://localhost/mcp", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    failing_after(
        monkeypatch,
        httpx.AsyncHTTPTransport,
        "aclose",
        lambda: httpx.CloseError("the close failed", request=request),
    )


def failing_after(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, failure: Callable[[], BaseException]
) -> None:
    original = getattr(owner, name)

    async def failing(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        raise failure()

    monkeypatch.setattr(owner, name, failing)


class Scene:
    def __init__(
        self,
        fake: McpAppFake,
        context: ssl.SSLContext,
        app_gate: threading.Event,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self.fake = fake
        self.context = context
        self.app_gate = app_gate
        self.monkeypatch = monkeypatch
        self.caplog = caplog
        self.clock = StepClock()
        self.listeners = ListenerRecorder(monkeypatch)
        self.stalls: list[Stall] = []
        self.stalls_held_weakly = False
        self._releases: list[Callable[[], None]] = []

    async def open(self) -> RunTools:
        return await RunTools.open(RUN_ID, self.fake.origin, CAPABILITY, self.context, self.clock)

    def stall(self, owner: type, name: str) -> Stall:
        stall = Stall(self.monkeypatch, owner, name, weakly=self.stalls_held_weakly)
        self.stalls.append(stall)
        return stall

    def on_release(self, release: Callable[[], None]) -> None:
        self._releases.append(release)

    def app_calls(self) -> list[dict[str, Any]]:
        return [
            request.message["params"]
            for request in self.fake.requests
            if request.rpc_method == "tools/call"
        ]

    async def release_and_drain(self) -> None:
        self.app_gate.set()
        self.fake.tail_released.set()
        for stall in self.stalls:
            stall.release()
        for release in self._releases:
            release()
        current = asyncio.current_task()
        leftovers = {
            task
            for task in asyncio.all_tasks()
            if task is not current
            and task.get_name().startswith((f"upstream:{RUN_ID}", f"relay:{RUN_ID}", "run-tools:"))
        }
        if leftovers:
            await asyncio.wait(leftovers, timeout=TEST_WAIT_SECONDS)

    def logged(self) -> list[tuple[str, str]]:
        gc.collect()
        return [(event, kind) for event, kind, _ in failure_lines(self.caplog)]

    async def app_saw_every_connection_end(self) -> None:
        await yield_until(lambda: self.fake.ended_connections == self.fake.accepted_connections)

    async def assert_nothing_left(self) -> None:
        assert relay_tasks() == []
        assert task_names(f"upstream:{RUN_ID}:") == []
        assert self.listeners.serving == []
        for port in self.listeners.ports:
            with pytest.raises(ConnectionRefusedError):
                await RawClient.connect(port)


def run_scene(
    scenario: Callable[[Scene], Awaitable[None]],
    authority: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    app_holds: Callable[[threading.Event], Mapping[str, Responder]] = lambda _gate: {},
    pages: list[dict[str, Any]] | None = None,
) -> None:
    gate = threading.Event()
    app = ScriptedApp(
        pages=pages or [tool_listing(OFFERED)],
        overrides=app_holds(gate),
    )
    with serving(app, authority.credentials_for(CERTIFICATE_HOST), keep_alive=True) as fake:
        scene = Scene(fake, authority.client_context(), gate, monkeypatch, caplog)

        async def main() -> None:
            try:
                await scenario(scene)
            finally:
                await scene.release_and_drain()

        with asyncio.Runner(loop_factory=SteppedLoop) as runner:
            runner.run(main())


async def finished(task: asyncio.Task[Any]) -> None:
    done, _ = await asyncio.wait({task}, timeout=TEST_WAIT_SECONDS)
    assert task in done


async def ending_of(task: asyncio.Task[Any]) -> object:
    await finished(task)
    (ending,) = await asyncio.gather(task, return_exceptions=True)
    return ending


async def through_parks(scene: Scene, parks: tuple[float, ...]) -> None:
    for instant in parks:
        await parked_at(scene.clock, instant)
        scene.clock.advance_to(instant)


def start_close(tools: RunTools, reap: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
    return asyncio.get_running_loop().create_task(tools.close(reap), name="test:closer")


def door_of(tools: RunTools) -> RelayDoor:
    return RelayDoor(tools.access)


def cli_call(tools: RunTools, request_id: int) -> bytes:
    params = {"name": "edit_song", "arguments": {}}
    body = json_rpc("tools/call", request_id, params)
    return post_message(door_of(tools), body, SUPPORTED)


async def reply_of(client: RawClient) -> dict[str, Any]:
    return json.loads((await client.answer()).body)


async def put_a_forwarded_call_on_the_barrier(scene: Scene, barrier: ToolCallBarrier) -> None:
    asyncio.get_running_loop().create_task(
        barrier.call(1, CallToolParams(name="edit_song", arguments={}))
    )
    barrier.accept(await within_the_bound(barrier.next_event()))
    await yield_until(lambda: scene.app_calls() != [])


def a_loss() -> ContainmentLost:
    return ContainmentLost(RetainedGroup(4242, 7))


class Cancellation(Enum):
    DIRECT = auto()
    LEVEL = auto()
