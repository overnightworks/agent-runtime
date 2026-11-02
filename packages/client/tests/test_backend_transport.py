"""``ServiceBackend`` against what the real service never sends: driven through a fake service.

Each test is a row of the connector's scenario matrix (M-AR1); the real service answers the
rest in the service suite.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.support.backend_support import (
    PERSON,
    PERSON_TOKEN,
    WORKLOAD_TOKEN,
    FakeService,
    RecordingLifecycle,
    a_backend,
    a_request,
    an_asker,
    frames,
    turn_outcome,
)
from tests.support.service_support import served
from tests.support.wire_builders import RUN_ID, call, started, text

from agent_runtime_client.backend import TurnStopKind
from agent_runtime_client.tools import ToolEffect
from agent_runtime_client.wire import (
    PROTOCOL_VERSION,
    FailureCode,
    RejectionCode,
)

REFUSAL_STATUS = 503


def _refusal_body(code: str) -> bytes:
    return json.dumps({"v": PROTOCOL_VERSION, "code": code, "message": "no"}).encode()


def _run(fake: FakeService, lifecycle: RecordingLifecycle | None = None, *, person: bool = True):
    with served(fake) as base_url:
        return asyncio.run(
            turn_outcome(a_backend(base_url, lifecycle), a_request(), an_asker(person=person))
        )


@pytest.mark.parametrize("code", [RejectionCode.NOT_FOUND, RejectionCode.CAPABILITY_TOO_LARGE])
def test_m5_15_a_code_the_run_route_cannot_be_driven_to_keeps_its_name(code):
    # The service answers not_found only for GET /runs/{id}, and capability_too_large only for
    # a capability past its size bound, which no honest request reaches.
    fake = FakeService(REFUSAL_STATUS, chunks=[_refusal_body(code)])

    events, stopped = _run(fake)

    assert (events, stopped.kind, stopped.code) == ([], TurnStopKind.REFUSED, code)


@pytest.mark.parametrize(
    "body",
    [b"<html>not json</html>", _refusal_body("brand_new_code")],
    ids=["m16-not-json", "m17-unknown-code"],
)
def test_m16_17_an_undecodable_or_unknown_refusal_is_unavailable(body):
    _, stopped = _run(FakeService(REFUSAL_STATUS, chunks=[body]))

    assert (stopped.kind, stopped.code) == (TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE)


@pytest.mark.parametrize(
    ("retry_after", "wait"),
    [("86400", 600), ("7", 7), ("soon", None), ("-3", None), (None, None)],
    ids=["m19-bounded", "kept", "m20-malformed", "negative", "absent"],
)
def test_m19_20_busy_carries_a_bounded_wait_or_none(retry_after, wait):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    fake = FakeService(429, headers, [_refusal_body("busy")])

    _, stopped = _run(fake)

    assert (stopped.code, stopped.retry_after_s) == (RejectionCode.BUSY, wait)


def test_m25_a_sequence_gap_after_run_started_cancels_the_turn():
    gapped = text(2, "skipped seq 1")
    fake = FakeService(chunks=frames(started(), gapped))

    events, stopped = _run(fake)

    assert events == []
    assert (stopped.kind, stopped.after_side_effect, stopped.detail) == (
        TurnStopKind.CANCELLED,
        False,
        "sequence_gap",
    )


def test_m26_loss_after_a_side_effecting_call_fails_the_turn_as_interrupted():
    fake = FakeService(
        chunks=frames(started(), call(1, "t1", ToolEffect.SIDE_EFFECTING)), drops=True
    )

    events, stopped = _run(fake)

    assert [event.type for event in events] == ["tool_call"]
    assert (stopped.kind, stopped.code, stopped.after_side_effect) == (
        TurnStopKind.FAILED,
        FailureCode.INTERRUPTED,
        True,
    )


def test_m30_a_dropped_stream_releases_the_registered_run_once():
    lifecycle = RecordingLifecycle()

    _, stopped = _run(FakeService(chunks=frames(started(), text(1, "hi")), drops=True), lifecycle)

    assert stopped.kind is TurnStopKind.CANCELLED
    assert (lifecycle.registered, lifecycle.released) == ([(RUN_ID, PERSON)], [RUN_ID])


def test_a_stream_that_drops_before_run_started_is_unavailable():
    _, stopped = _run(FakeService(chunks=[b"id: 0\n"], drops=True))

    assert (stopped.kind, stopped.code) == (TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE)


def test_a_refused_registration_ends_unavailable_and_releases_nothing():
    lifecycle = RecordingLifecycle(held_elsewhere=[RUN_ID])

    _, stopped = _run(FakeService(chunks=frames(started())), lifecycle)

    assert (stopped.kind, stopped.code) == (TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE)
    assert (lifecycle.registered, lifecycle.released) == ([], [])


@pytest.mark.parametrize("person", [True, False])
def test_the_run_sends_the_workload_token_and_the_persons_when_there_is_one(person):
    fake = FakeService(REFUSAL_STATUS, chunks=[_refusal_body("unavailable")])

    _run(fake, person=person)

    (sent,) = fake.seen
    assert sent["agent-workload-token"] == WORKLOAD_TOKEN
    assert sent.get("authorization") == (f"Bearer {PERSON_TOKEN}" if person else None)


def test_no_token_reaches_the_text_a_stopped_turn_carries():
    _, stopped = _run(FakeService(chunks=frames(started()), drops=True))

    shown = f"{stopped!s} {stopped.detail} {stopped.__context__} {stopped.__cause__}"
    assert PERSON_TOKEN not in shown
    assert WORKLOAD_TOKEN not in shown
