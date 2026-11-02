"""``ServiceBackend`` against the real service app, served on a loopback socket.

Each test is a row of the connector's scenario matrix (M-AR1). The runner is a fake; the
tokens, admission, refusals and event stream are the service's own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import aclosing, contextmanager

import pytest
from pydantic import SecretStr
from tests.support.backend_support import (
    FakeService,
    RecordingLifecycle,
    a_backend,
    a_request,
    an_asker,
    frames,
    turn_outcome,
)
from tests.support.bounds import TEST_WAIT_SECONDS, within_the_bound
from tests.support.service_cli_runner_support import eventually
from tests.support.service_support import (
    APP_GROUP,
    PRESENTATOR_CLIENT,
    TEST_ISSUER,
    GatedRunner,
    ScriptedRunner,
    TokenMint,
    served,
    service_config,
)
from tests.support.wire_builders import started
from webauth.https_client import KeySetFetchError

from agent_runtime_client.backend import Asker, RunTokens, TurnStopKind
from agent_runtime_client.capability import CapabilityPrincipal
from agent_runtime_client.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_runtime_client.wire import (
    PROTOCOL_VERSION,
    Cancelled,
    FailureCode,
    RejectionCode,
    RunFile,
)
from agent_runtime_service.app import RETRY_BUSY_AFTER_SECONDS, create_app
from agent_runtime_service.runners import Runners

MINT = TokenMint()
ALICE = CapabilityPrincipal(iss=TEST_ISSUER, sub="user-alice")
ANSWER = (AssistantTextEvent(text="thinking"), FinalEvent(text="done"))
TOOLS = {"read_note": "read_only", "edit_note": "side_effecting"}


def _asker(person_token: str | None = None) -> Asker:
    async def tokens() -> RunTokens:
        return RunTokens(SecretStr(person_token or MINT.user()), SecretStr(MINT.workload()))

    return Asker(principal=ALICE, tokens=tokens)


@contextmanager
def _service(
    runner: object, *, runs: dict | None = None, fetch=MINT.fetch_key_set
) -> Iterator[str]:
    app = create_app(service_config(runs=runs), runners=Runners(cli=runner), fetch_key_set=fetch)
    with served(app) as base_url:
        yield base_url


def _turn(base_url: str, request=None, asker=None, lifecycle=None):
    return asyncio.run(
        turn_outcome(a_backend(base_url, lifecycle), request or a_request(), asker or _asker())
    )


def _lifecycle_events(lifecycle: RecordingLifecycle) -> tuple[int, int]:
    return len(lifecycle.registered), len(lifecycle.released)


def _unlisted(request):
    return request.model_copy(update={"model": "anthropic/unlisted"})


def _oversized(request):
    turn = request.turn.model_construct(system="x" * 300_000, messages=request.turn.messages)
    return request.model_copy(update={"turn": turn})


# Refused at the entrance by what the request or the tokens say, or by what the service cannot
# reach; not_found and capability_too_large are driven through the fake service.
REFUSALS = [
    pytest.param(RejectionCode.NOT_ENABLED, lambda r: r, {"groups": [APP_GROUP]}, id="not_enabled"),
    pytest.param(
        RejectionCode.UNSUPPORTED_VERSION,
        lambda r: r.model_copy(update={"v": 1}),
        {},
        id="unsupported_version",
    ),
    pytest.param(RejectionCode.UNAUTHORIZED, lambda r: r, {"signature": "bad"}, id="unauthorized"),
    pytest.param(RejectionCode.FORBIDDEN, lambda r: r, {"azp": PRESENTATOR_CLIENT}, id="forbidden"),
    pytest.param(
        RejectionCode.INVALID_REQUEST,
        lambda r: r.model_copy(update={"agent": "NOT VALID"}),
        {},
        id="invalid_request",
    ),
    pytest.param(RejectionCode.TOO_LARGE, _oversized, {}, id="too_large"),
    pytest.param(RejectionCode.MODEL_NOT_OFFERED, _unlisted, {}, id="model_not_offered"),
]


@pytest.mark.parametrize(("code", "shape", "claims"), REFUSALS)
def test_m5_15_each_refusal_the_service_sends_keeps_its_name(code, shape, claims):
    signature = claims.get("signature")
    user_claims = {k: v for k, v in claims.items() if k != "signature"}
    person = MINT.user(**user_claims) + ("x" if signature else "")
    with _service(ScriptedRunner(ANSWER)) as base_url:
        events, stopped = _turn(base_url, shape(a_request()), _asker(person))

    assert (events, stopped.kind, stopped.code) == ([], TurnStopKind.REFUSED, code)


def test_m5_15_an_unreachable_key_set_is_unavailable():
    def unreachable(url: str) -> bytes:
        raise KeySetFetchError

    with _service(ScriptedRunner(ANSWER), fetch=unreachable) as base_url:
        _, stopped = _turn(base_url)

    assert (stopped.kind, stopped.code) == (TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE)


def test_m18_busy_carries_the_services_wait():
    runner = GatedRunner()
    with _service(runner, runs={"max_parallel_cli_runs": 1}) as base_url:
        asyncio.run(_busy_while_a_run_is_held(base_url, runner))


async def _until(condition) -> None:
    async def holds() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await within_the_bound(holds())


async def _busy_while_a_run_is_held(base_url: str, runner: GatedRunner) -> None:
    async with aclosing(a_backend(base_url).run(a_request(), _asker())) as holding:
        pending = asyncio.ensure_future(anext(holding))
        await _until(lambda: runner.steps == 1)
        _, stopped = await turn_outcome(a_backend(base_url), a_request(), _asker())
        runner.release(AssistantTextEvent(text="go"))
        await within_the_bound(pending)
    assert (stopped.code, stopped.retry_after_s) == (RejectionCode.BUSY, RETRY_BUSY_AFTER_SECONDS)


def test_m21_events_map_and_files_are_kept():
    file = RunFile(name="notes.txt", content=b"la la")
    script = (
        AssistantTextEvent(text="hello"),
        ToolCallEvent(tool_use_id="t1", name="read_note", input={"id": 1}),
        ToolResultEvent(tool_use_id="t1", content="ok"),
        FinalEvent(text="done", files=(file,)),
    )
    with _service(ScriptedRunner(script)) as base_url:
        events, stopped = _turn(base_url, a_request(tools=TOOLS))

    assert stopped is None
    assert events == list(script)


class _HeartbeatWatch:
    """An ASGI wrapper that notes when a heartbeat frame leaves the service on a response."""

    def __init__(self, app) -> None:
        self._app = app
        self.sent = asyncio.Event()

    async def __call__(self, scope, receive, send) -> None:
        async def watching(message) -> None:
            body = message.get("body", b"")
            if b"event: heartbeat" in body:
                self.sent.set()
            await send(message)

        await self._app(scope, receive, watching)


def test_m22_heartbeats_never_surface():
    watch_ref: list[_HeartbeatWatch] = []

    class Pausing(ScriptedRunner):
        async def run(self, admitted):
            yield AssistantTextEvent(text="before")
            await asyncio.wait_for(watch_ref[0].sent.wait(), TEST_WAIT_SECONDS)
            yield FinalEvent(text="after")

    runs = {"heartbeat_interval_s": 1, "stall_timeout_s": 2.0, "idle_timeout_s": 4.0}
    app = create_app(
        service_config(runs=runs),
        runners=Runners(cli=Pausing([])),
        fetch_key_set=MINT.fetch_key_set,
    )
    watch_ref.append(_HeartbeatWatch(app))
    with served(watch_ref[0]) as base_url:
        events, stopped = _turn(base_url)

    assert watch_ref[0].sent.is_set()
    assert (stopped, [event.type for event in events]) == (None, ["assistant_text", "final"])


@pytest.mark.parametrize("token", ["bad\ntoken", "bad\rtoken", "bad\x00token", "bäd"])
def test_a_token_no_header_can_carry_ends_the_turn_refused_without_a_request(token):
    fake = FakeService()
    with served(fake) as base_url:
        events, stopped = _turn(base_url, asker=_asker(token))

    assert (events, fake.seen) == ([], [])
    assert (stopped.kind, stopped.code) == (TurnStopKind.REFUSED, RejectionCode.UNAUTHORIZED)
    assert token not in f"{stopped} {stopped.detail}"


def test_m23_a_failed_run_keeps_its_code():
    with _service(ScriptedRunner((AssistantTextEvent(text="no answer"),))) as base_url:
        events, stopped = _turn(base_url)

    assert [event.type for event in events] == ["assistant_text"]
    assert (stopped.kind, stopped.code) == (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)


def test_m24_a_cancelled_run_ends_the_turn_as_cancelled():
    cancelled = Cancelled(v=PROTOCOL_VERSION, run_id=started().run_id, seq=1)
    with served(FakeService(chunks=frames(started(), cancelled))) as base_url:
        _, stopped = _turn(base_url)

    assert stopped.kind is TurnStopKind.CANCELLED


def test_m27_28_29_the_lifecycle_registers_once_and_releases_after_each_terminal():
    for script, kind in ((ANSWER, None), (ANSWER[:1], TurnStopKind.FAILED)):
        lifecycle = RecordingLifecycle()
        with _service(ScriptedRunner(script)) as base_url:
            _, stopped = _turn(base_url, lifecycle=lifecycle)

        assert (stopped and stopped.kind) == kind
        ((run_id, principal),) = lifecycle.registered
        assert (principal, lifecycle.released) == (ALICE, [run_id])


def test_m31_leaving_aclosing_after_the_first_event_releases_and_closes_the_response():
    runner = GatedRunner()
    lifecycle = RecordingLifecycle()

    async def leave_after_first_event() -> None:
        runner.release(AssistantTextEvent(text="first"))
        async with aclosing(a_backend(base_url, lifecycle).run(a_request(), _asker())) as run:
            await within_the_bound(anext(run))

    with _service(runner) as base_url:
        asyncio.run(leave_after_first_event())
        eventually(lambda: runner.closes == 1)

    assert _lifecycle_events(lifecycle) == (1, 1)


def test_m32_cancelling_the_consuming_task_after_run_started_releases_once():
    runner = GatedRunner()
    lifecycle = RecordingLifecycle()

    async def cancel_after_registration() -> None:
        consuming = asyncio.create_task(
            turn_outcome(a_backend(base_url, lifecycle), a_request(), _asker())
        )
        await _until(lambda: bool(lifecycle.registered))
        consuming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consuming

    with _service(runner) as base_url:
        asyncio.run(cancel_after_registration())
        eventually(lambda: runner.closes == 1)

    assert _lifecycle_events(lifecycle) == (1, 1)


def test_m33a_a_generator_never_iterated_opens_no_request():
    runner = ScriptedRunner(ANSWER)
    lifecycle = RecordingLifecycle()

    async def close_unstarted() -> None:
        await a_backend(base_url, lifecycle).run(a_request(), _asker()).aclose()

    with _service(runner) as base_url:
        asyncio.run(close_unstarted())

    assert (runner.admitted, _lifecycle_events(lifecycle)) == ([], (0, 0))


def test_m33b_cancelling_a_pending_start_closes_the_response_and_registers_nothing():
    fake = FakeService(silent=True)
    lifecycle = RecordingLifecycle()

    async def cancel_while_waiting() -> None:
        consuming = asyncio.create_task(
            turn_outcome(a_backend(base_url, lifecycle), a_request(), an_asker())
        )
        await within_the_bound(asyncio.to_thread(_wait_for_request, fake))
        consuming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consuming

    with served(fake) as base_url:
        asyncio.run(cancel_while_waiting())
        assert fake.disconnected.wait(TEST_WAIT_SECONDS)

    assert _lifecycle_events(lifecycle) == (0, 0)


def _wait_for_request(fake: FakeService) -> None:
    eventually(lambda: bool(fake.seen))


def test_m34_a_refusal_at_the_post_registers_nothing():
    lifecycle = RecordingLifecycle()
    with _service(ScriptedRunner(ANSWER)) as base_url:
        _, stopped = _turn(base_url, asker=_asker(MINT.user() + "x"), lifecycle=lifecycle)

    assert stopped.code is RejectionCode.UNAUTHORIZED
    assert _lifecycle_events(lifecycle) == (0, 0)


def test_m35_a_refused_registration_ends_unavailable_and_releases_nothing():
    lifecycle = _HeldByAnotherTurn()
    with _service(ScriptedRunner(ANSWER)) as base_url:
        events, stopped = _turn(base_url, lifecycle=lifecycle)

    assert (events, stopped.kind, stopped.code) == (
        [],
        TurnStopKind.REFUSED,
        RejectionCode.UNAVAILABLE,
    )
    assert (lifecycle.released, len(lifecycle.registered)) == ([], 0)


class _HeldByAnotherTurn(RecordingLifecycle):
    """A port whose run ids are all held by another turn."""

    def register(self, run_id, principal) -> None:
        self.held_elsewhere.add(run_id)
        super().register(run_id, principal)


def test_m36_the_token_source_is_asked_once_and_its_refusal_passes_through():
    asked: list[None] = []
    with _service(ScriptedRunner(ANSWER)) as base_url:
        _turn(base_url, asker=an_asker(tokens_asked=asked))

    class SignInAgain(Exception):
        pass

    async def refusing() -> RunTokens:
        raise SignInAgain

    with _service(ScriptedRunner(ANSWER)) as base_url, pytest.raises(SignInAgain):
        _turn(base_url, asker=Asker(principal=None, tokens=refusing))
    assert len(asked) == 1
