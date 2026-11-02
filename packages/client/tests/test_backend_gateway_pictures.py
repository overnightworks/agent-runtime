"""``GatewayBackend`` running a picture agent against a scripted gateway: the cells of M-P."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from tests.support.gateway_turn_support import (
    SkewedLoop,
    a_gateway_config,
    a_person,
    ending,
)

from agent_runtime_client.agents import TurnRequest
from agent_runtime_client.backend import TurnPerson, TurnStopKind, TurnStopped
from agent_runtime_client.backend.fake_gateway import FakeGateway, Reply, completion, picture
from agent_runtime_client.backend.gateway import GatewayBackend
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.turn import (
    MAX_EVENT_BYTES,
    MAX_RUN_FILES_BYTES,
    FailureCode,
    RejectionCode,
    RunFile,
    Turn,
    TurnMessage,
    TurnRole,
    base64_length,
)
from agent_runtime_client.turn_events import FinalEvent

PROVIDER_ERROR = (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)
UNAVAILABLE = (TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE)
LIMIT_EXCEEDED = (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
BUSY = (TurnStopKind.REFUSED, RejectionCode.BUSY)
TURN_BUDGET_SECONDS = 30


def a_picture_request(**changes) -> TurnRequest:
    fields = {
        "agent": "painter",
        "turn": Turn(system="Paint it.", messages=[TurnMessage(role=TurnRole.USER, text="a cat")]),
        "roles": ["admin", "user"],
        "tools": (),
        "model": "image/standin",
        "max_duration_seconds": TURN_BUDGET_SECONDS,
        "max_tool_calls": None,
        "picture_size": "1024x1024",
    }
    return TurnRequest(**{**fields, **changes})


def a_json_reply(body: object) -> Reply:
    return Reply(body=json.dumps(body).encode())


def an_entry(content: bytes = b"PNG", **extra) -> dict:
    return {"b64_json": base64.b64encode(content).decode("ascii"), **extra}


@dataclass
class Studio:
    """The app side of a picture turn: its key file, its person and the gateway."""

    directory: Path

    def config(self) -> AgentConfig:
        return a_gateway_config(self.directory)

    def run(self, *replies: Reply, request: TurnRequest | None = None, person=None, transport=None):
        gateway = FakeGateway(*replies) if transport is None else transport
        backend = GatewayBackend(self.config(), transport=gateway)
        with asyncio.Runner(loop_factory=SkewedLoop) as runner:
            events, stopped = runner.run(
                _turn(backend, request or a_picture_request(), person or a_person())
            )
        return events, stopped, gateway


async def _turn(backend: GatewayBackend, request: TurnRequest, person: TurnPerson):
    events = []
    try:
        async with contextlib.aclosing(backend.run(request, person)) as stream:
            async for event in stream:
                events.append(event)
    except TurnStopped as stopped:
        return events, stopped
    return events, None


@pytest.fixture
def studio(tmp_path: Path) -> Studio:
    return Studio(tmp_path)


def test_the_call_is_one_post_to_the_images_path_with_the_headers_of_a_text_turn(studio):
    _, _, text_gateway = studio.run(
        completion("ok"), request=a_picture_request(picture_size=None, model="text/standin")
    )
    _, _, gateway = studio.run(picture(b"PNG"))

    (sent,) = gateway.requests
    (text_sent,) = text_gateway.requests
    assert (sent.method, sent.url.path, sent.url.host) == (
        "POST",
        "/v1/images/generations",
        "gateway.test",
    )
    assert dict(sent.headers) | {"content-length": ""} == dict(text_sent.headers) | {
        "content-length": ""
    }


def test_the_body_holds_exactly_model_prompt_size_and_user(studio):
    _, _, gateway = studio.run(picture(b"PNG"))

    body = json.loads(gateway.requests[0].content)
    assert body == {
        "model": "image/standin",
        "prompt": "Paint it.\n\na cat",
        "size": "1024x1024",
        "user": body["user"],
    }


def test_the_user_is_the_fingerprint_a_text_turn_of_the_same_person_sends(studio):
    _, _, text_gateway = studio.run(
        completion("ok"), request=a_picture_request(picture_size=None, model="text/standin")
    )
    _, _, gateway = studio.run(picture(b"PNG"))

    assert (
        json.loads(gateway.requests[0].content)["user"]
        == (json.loads(text_gateway.requests[0].content)["user"])
    )


def test_one_picture_ends_the_turn_as_a_file_with_no_other_event(studio):
    events, stopped, _ = studio.run(picture(b"\x89PNG bytes"))

    assert stopped is None
    assert events == [
        FinalEvent(text="", files=(RunFile(name="picture", content=b"\x89PNG bytes"),))
    ]


def test_a_null_url_beside_the_picture_passes(studio):
    events, stopped, _ = studio.run(a_json_reply({"data": [an_entry(url=None)]}))

    assert (stopped, [type(event) for event in events]) == (None, [FinalEvent])


@pytest.mark.parametrize(
    "reply",
    [
        a_json_reply({"data": [an_entry(url="http://elsewhere.test/picture.png")]}),
        a_json_reply({"data": [{"url": "http://elsewhere.test/picture.png"}]}),
        a_json_reply({"data": [an_entry(), an_entry()]}),
        a_json_reply({"data": []}),
        a_json_reply({"data": an_entry()}),
        a_json_reply({"data": [{"b64_json": "!!!not base64"}]}),
        a_json_reply({"data": [{"b64_json": "UE5H"[:-1]}]}),
        a_json_reply({"data": [{"b64_json": " UE5H"}]}),
        a_json_reply({"data": [{"b64_json": "UE5H\n"}]}),
        a_json_reply({"data": [{"b64_json": "UE-_"}]}),
        a_json_reply({"data": [{"b64_json": "UE5="}]}),
        a_json_reply({"data": [{"b64_json": 5}]}),
        a_json_reply({"data": [{"b64_json": None}]}),
        a_json_reply({"data": [{"b64_json": ""}]}),
        a_json_reply({"data": ["UE5H"]}),
        a_json_reply({}),
        a_json_reply([]),
        Reply(body=b"not json"),
        Reply(body=b""),
    ],
)
def test_a_reply_that_is_not_exactly_one_canonical_picture_is_a_provider_error(studio, reply):
    events, stopped, gateway = studio.run(reply, picture(b"never"))

    assert (events, ending(stopped), len(gateway.requests)) == ([], PROVIDER_ERROR, 1)


def test_a_non_null_url_is_never_fetched(studio):
    url = "http://elsewhere.test/picture.png"

    _, stopped, gateway = studio.run(a_json_reply({"data": [an_entry(url=url)]}), picture(b"x"))

    assert ending(stopped) == PROVIDER_ERROR
    assert [str(request.url) for request in gateway.requests] == [
        "http://gateway.test/v1/images/generations"
    ]


def padded_reply(content: bytes, total_bytes: int) -> Reply:
    """A valid reply whose body is exactly ``total_bytes``, JSON whitespace making up the rest."""
    body = json.dumps({"data": [an_entry(content)]}).encode()
    return Reply(body=body + b" " * (total_bytes - len(body)))


ENVELOPE_BOUND = base64_length(MAX_RUN_FILES_BYTES) + MAX_EVENT_BYTES


def test_a_body_at_the_read_bound_passes(studio):
    _, stopped, _ = studio.run(padded_reply(b"PNG", ENVELOPE_BOUND))

    assert stopped is None


def test_a_body_one_byte_over_the_read_bound_is_a_provider_error(studio):
    _, stopped, _ = studio.run(padded_reply(b"PNG", ENVELOPE_BOUND + 1))

    assert ending(stopped) == PROVIDER_ERROR


def test_a_picture_at_its_decoded_bound_passes(studio):
    events, stopped, _ = studio.run(picture(bytes(MAX_RUN_FILES_BYTES)))

    assert (stopped, len(events[0].files[0].content)) == (None, MAX_RUN_FILES_BYTES)


def test_a_picture_one_byte_over_its_decoded_bound_is_a_provider_error(studio):
    _, stopped, _ = studio.run(picture(bytes(MAX_RUN_FILES_BYTES + 1)))

    assert ending(stopped) == PROVIDER_ERROR


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (Reply(429, {"Retry-After": "7"}), BUSY),
        (Reply(400), UNAVAILABLE),
        (Reply(401), UNAVAILABLE),
        (Reply(500), UNAVAILABLE),
        (Reply(302, {"Location": "http://elsewhere.test/"}), UNAVAILABLE),
    ],
)
def test_a_refusing_status_ends_as_for_a_text_turn_with_no_later_request(studio, reply, expected):
    events, stopped, gateway = studio.run(reply, picture(b"never"))

    assert (events, ending(stopped), len(gateway.requests)) == ([], expected, 1)


def test_a_busy_refusal_carries_the_wait(studio):
    _, stopped, _ = studio.run(Reply(429, {"Retry-After": "7"}))

    assert stopped.retry_after_s == 7


class _Failing(httpx2.AsyncBaseTransport):
    def __init__(self) -> None:
        self.attempts = 0

    async def handle_async_request(self, request):
        self.attempts += 1
        raise httpx2.ConnectError("refused")


def test_a_lost_connection_is_provider_unavailable_with_no_retry(studio):
    failing = _Failing()

    _, stopped, _ = studio.run(transport=failing)

    assert (ending(stopped), failing.attempts) == (UNAVAILABLE, 1)


class _Hanging(httpx2.AsyncBaseTransport):
    """Takes the request and never answers; it notes whether the call was cancelled."""

    def __init__(self, *, seconds_passing: float = 0) -> None:
        self.attempts = 0
        self.cancelled = False
        self._seconds_passing = seconds_passing

    async def handle_async_request(self, request):
        self.attempts += 1
        asyncio.get_running_loop().skew_seconds += self._seconds_passing
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_a_call_past_the_deadline_is_limit_exceeded_and_cancelled_with_no_retry(studio):
    slow = _Hanging(seconds_passing=TURN_BUDGET_SECONDS + 1)

    events, stopped, _ = studio.run(transport=slow)

    assert (events, ending(stopped), slow.attempts, slow.cancelled) == ([], LIMIT_EXCEEDED, 1, True)


def test_a_deadline_passed_during_admission_is_limit_exceeded_with_no_request(studio):
    async def member_of() -> frozenset[str]:
        asyncio.get_running_loop().skew_seconds += TURN_BUDGET_SECONDS + 1
        return await a_person().groups()

    late = TurnPerson(principal=a_person().principal, role="user", groups=member_of)

    events, stopped, gateway = studio.run(picture(b"PNG"), person=late)

    assert (events, ending(stopped), gateway.requests) == ([], LIMIT_EXCEEDED, [])


def test_closing_the_stream_while_the_call_is_open_cancels_it_and_sends_no_other(studio):
    slow = _Hanging()
    backend = GatewayBackend(studio.config(), transport=slow)

    async def close_open_call() -> None:
        stream = backend.run(a_picture_request(), a_person())
        task = asyncio.ensure_future(anext(stream))
        while not slow.attempts:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()

    with asyncio.Runner(loop_factory=SkewedLoop) as runner:
        runner.run(close_open_call())

    assert (slow.attempts, slow.cancelled) == (1, True)


@pytest.mark.parametrize(
    ("person", "expected"),
    [
        (a_person(frozenset({"/apps/other"})), RejectionCode.NOT_ENABLED),
        (a_person(frozenset()), RejectionCode.NOT_ENABLED),
    ],
)
def test_a_person_outside_the_api_group_sends_no_request(studio, person, expected):
    _, stopped, gateway = studio.run(picture(b"PNG"), person=person)

    assert (stopped.kind, stopped.code, gateway.requests) == (TurnStopKind.REFUSED, expected, [])


def test_a_role_the_agent_is_not_offered_to_sends_no_request(studio):
    request = a_picture_request(roles=["admin"])

    _, stopped, gateway = studio.run(picture(b"PNG"), request=request)

    assert (stopped.code, gateway.requests) == (RejectionCode.FORBIDDEN, [])


def test_a_request_over_the_text_turns_request_bound_is_limit_exceeded_and_sends_nothing(studio):
    turn = Turn(
        system="x" * (4 * MAX_EVENT_BYTES),
        messages=[TurnMessage(role=TurnRole.USER, text="a cat")],
    )

    _, stopped, gateway = studio.run(picture(b"PNG"), request=a_picture_request(turn=turn))

    assert (ending(stopped), gateway.requests) == (LIMIT_EXCEEDED, [])


def test_a_text_agents_request_is_unchanged(studio):
    text_request = a_picture_request(picture_size=None, model="text/standin")

    events, stopped, gateway = studio.run(completion("ok"), request=text_request)

    body = json.loads(gateway.requests[0].content)
    assert (stopped, gateway.requests[0].url.path, set(body)) == (
        None,
        "/v1/chat/completions",
        {"model", "messages", "user"},
    )
    assert events[-1] == FinalEvent(text="ok")
