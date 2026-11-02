"""``GatewayBackend`` against a scripted gateway: the rows of the gateway matrix (M-L1)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from agent_runtime_client.agents import TurnRequest
from agent_runtime_client.backend import MAX_RETRY_AFTER_S, TurnPerson, TurnStopKind, TurnStopped
from agent_runtime_client.backend.fake_gateway import FakeGateway, Reply, completion
from agent_runtime_client.backend.gateway import GatewayBackend
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.tools import ToolDeclaration
from agent_runtime_client.turn import (
    MAX_EVENT_BYTES,
    FailureCode,
    Principal,
    RejectionCode,
    Turn,
    TurnMessage,
    TurnRole,
)
from agent_runtime_client.turn_events import AssistantTextEvent, FinalEvent

KEY = "stand-in-key-0001"
API_GROUP = "/apps/example"
ISSUER = "https://issuer.example"
SUBJECT = "subject-1"
FIXED_FINGERPRINT = "fp-2db44d96ffdef38f9e1cbfe73e9cf248fb66955b894846b34843a2d3a849d764"
TRANSPORT_DEFAULT_HEADERS = {
    "host",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
    "content-length",
    "content-type",
    "authorization",
}
PINNED_ABSENT_BODY_KEYS = {
    "metadata",
    "litellm_metadata",
    "tags",
    "fallbacks",
    "num_retries",
    "mock_response",
    "api_key",
    "stream",
    "max_tokens",
    "tools",
}


def a_config(tmp_path: Path, *, origin: str = "http://gateway.test", key: bytes | None = None):
    key_file = tmp_path / "key"
    key_file.write_bytes(f"{KEY}\n".encode() if key is None else key)
    section = {
        "backend": "litellm",
        "gateway": {"origin": origin, "key_file": str(key_file)},
        "api_group": API_GROUP,
    }
    return AgentConfig.model_validate(section)


def a_request(**changes) -> TurnRequest:
    turn = Turn(
        system="Answer briefly.",
        messages=[
            TurnMessage(role=TurnRole.USER, text="Hello"),
            TurnMessage(role=TurnRole.ASSISTANT, text="Hi"),
            TurnMessage(role=TurnRole.USER, text="Again"),
        ],
    )
    fields = {
        "agent": "helper",
        "turn": turn,
        "roles": ["admin", "user"],
        "tools": (),
        "model": "text/standin",
        "max_duration_seconds": 30,
    }
    return TurnRequest(**{**fields, **changes})


def a_person(groups=frozenset({API_GROUP}), *, iss=ISSUER, sub=SUBJECT, role="user"):
    calls = []

    async def member_of() -> frozenset[str]:
        calls.append(1)
        if isinstance(groups, Exception):
            raise groups
        return groups

    person = TurnPerson(principal=Principal(iss=iss, sub=sub), role=role, groups=member_of)
    return person, calls


async def _turn(backend, request, person):
    events = []
    try:
        async with contextlib.aclosing(backend.run(request, person)) as stream:
            async for event in stream:
                events.append(event)
    except TurnStopped as stopped:
        return events, stopped
    return events, None


def outcome(backend, request=None, person=None):
    return asyncio.run(_turn(backend, request or a_request(), person or a_person()[0]))


def ending(stopped: TurnStopped | None):
    return None if stopped is None else (stopped.kind, stopped.code)


UNAVAILABLE = (TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE)
PROVIDER_ERROR = (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)


def test_a_text_turn_ends_with_the_answer(tmp_path):
    gateway = FakeGateway(completion("The answer."))

    events, stopped = outcome(GatewayBackend(a_config(tmp_path), transport=gateway))

    assert stopped is None
    assert events == [AssistantTextEvent(text="The answer."), FinalEvent(text="The answer.")]


def test_the_request_is_one_post_with_the_system_prompt_and_the_messages(tmp_path):
    gateway = FakeGateway(completion("ok"))

    outcome(GatewayBackend(a_config(tmp_path), transport=gateway))

    (sent,) = gateway.requests
    assert (sent.method, sent.url.path, sent.url.host) == (
        "POST",
        "/v1/chat/completions",
        "gateway.test",
    )
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["content-type"] == "application/json"
    body = json.loads(sent.content)
    assert set(body) == {"model", "messages", "user"}
    assert body["model"] == "text/standin"
    assert body["messages"] == [
        {"role": "system", "content": "Answer briefly."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "Again"},
    ]


def test_no_header_beyond_the_transports_own_defaults_is_sent(tmp_path):
    gateway = FakeGateway(completion("ok"))

    outcome(GatewayBackend(a_config(tmp_path), transport=gateway))

    assert {name.lower() for name in gateway.requests[0].headers} <= TRANSPORT_DEFAULT_HEADERS


def test_no_pinned_absent_field_is_sent(tmp_path):
    gateway = FakeGateway(completion("ok"))

    outcome(GatewayBackend(a_config(tmp_path), transport=gateway))

    text = gateway.requests[0].content.decode()
    assert not PINNED_ABSENT_BODY_KEYS & set(json.loads(text))
    assert not re.search(r"extra_|x-litellm|anthropic-", text)


def test_the_user_field_is_the_fixed_fingerprint_of_the_principal(tmp_path):
    gateway = FakeGateway(completion("ok"))

    outcome(GatewayBackend(a_config(tmp_path), transport=gateway))

    assert json.loads(gateway.requests[0].content)["user"] == FIXED_FINGERPRINT


def _fingerprint_of(tmp_path, iss, sub) -> str:
    gateway = FakeGateway(completion("ok"))
    person, _ = a_person(iss=iss, sub=sub)
    outcome(GatewayBackend(a_config(tmp_path), transport=gateway), person=person)
    return json.loads(gateway.requests[0].content)["user"]


@pytest.mark.parametrize(
    ("other_iss", "other_sub"),
    [(ISSUER + "/", SUBJECT), (ISSUER, SUBJECT + "x"), ("https://issuer.exampl", "e" + SUBJECT)],
    ids=["issuer", "subject", "pair-colliding-under-concatenation"],
)
def test_a_different_principal_gets_a_different_fingerprint(tmp_path, other_iss, other_sub):
    assert _fingerprint_of(tmp_path, ISSUER, SUBJECT) != _fingerprint_of(
        tmp_path, other_iss, other_sub
    )


def test_a_fingerprint_is_67_characters_and_the_same_for_one_principal(tmp_path):
    first = _fingerprint_of(tmp_path, ISSUER, SUBJECT)

    assert re.fullmatch(r"fp-[0-9a-f]{64}", first)
    assert first == _fingerprint_of(tmp_path, ISSUER, SUBJECT)


def test_a_role_outside_the_agents_roles_is_forbidden_and_sends_nothing(tmp_path):
    gateway = FakeGateway(completion("ok"))
    person, calls = a_person(role="user")

    _, stopped = outcome(
        GatewayBackend(a_config(tmp_path), transport=gateway), a_request(roles=["admin"]), person
    )

    assert ending(stopped) == (TurnStopKind.REFUSED, RejectionCode.FORBIDDEN)
    assert (gateway.requests, calls) == ([], [])


def test_a_group_the_person_lacks_is_not_enabled_and_sends_nothing(tmp_path):
    gateway = FakeGateway(completion("ok"))
    person, calls = a_person(frozenset({"/apps/other"}))

    _, stopped = outcome(GatewayBackend(a_config(tmp_path), transport=gateway), person=person)

    assert ending(stopped) == (TurnStopKind.REFUSED, RejectionCode.NOT_ENABLED)
    assert (gateway.requests, calls) == ([], [1])


def test_a_refusal_the_groups_lookup_raises_reaches_the_app_unchanged(tmp_path):
    gateway = FakeGateway(completion("ok"))
    refusal = TurnStopped(TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE)
    person, calls = a_person(refusal)

    _, stopped = outcome(GatewayBackend(a_config(tmp_path), transport=gateway), person=person)

    assert stopped is refusal
    assert (gateway.requests, calls) == ([], [1])


def test_groups_are_asked_once_per_turn(tmp_path):
    person, calls = a_person()

    backend = GatewayBackend(a_config(tmp_path), transport=FakeGateway(completion("ok")))
    outcome(backend, person=person)

    assert calls == [1]


def test_an_agent_with_tools_raises_before_any_request(tmp_path):
    gateway = FakeGateway(completion("ok"))
    tool = ToolDeclaration(name="lookup", description="Look something up.", parameters={})
    backend = GatewayBackend(a_config(tmp_path), transport=gateway)

    with pytest.raises(ValueError, match="tools"):
        outcome(backend, a_request(tools=(tool,)))

    assert gateway.requests == []


def test_a_generator_never_iterated_opens_nothing_and_reads_no_key(tmp_path):
    config = a_config(tmp_path)
    config.gateway.key_file.unlink()
    gateway = FakeGateway(completion("ok"))

    backend = GatewayBackend(config, transport=gateway)
    backend.run(a_request(), a_person()[0])

    assert gateway.requests == []


@pytest.mark.parametrize(
    "key",
    [None, b"x" * 100_000, b"first\nsecond\n", b"", b"has space\n", "kéy\n".encode()],
    ids=["missing", "oversized", "two-lines", "empty", "space", "non-ascii"],
)
def test_an_unusable_key_file_is_provider_unavailable_and_sends_nothing(tmp_path, key):
    config = a_config(tmp_path, key=key or b"")
    if key is None:
        config.gateway.key_file.unlink()
    gateway = FakeGateway(completion("ok"))

    _, stopped = outcome(GatewayBackend(config, transport=gateway))

    assert ending(stopped) == UNAVAILABLE
    assert gateway.requests == []
    assert KEY not in repr(stopped.args) and "second" not in repr(stopped.args)


def test_a_key_with_any_prefix_is_sent_as_it_is(tmp_path):
    gateway = FakeGateway(completion("ok"))

    outcome(GatewayBackend(a_config(tmp_path, key=b"sk-anything/+=\n"), transport=gateway))

    assert gateway.requests[0].headers["authorization"] == "Bearer sk-anything/+="


@pytest.mark.parametrize(
    "reply",
    [Reply(401), Reply(403), Reply(422, body=b'{"error":"budget_exceeded"}'), Reply(404)],
)
def test_a_refusal_status_is_provider_unavailable_as_gateway_refused(tmp_path, reply, caplog):
    caplog.set_level(logging.INFO)

    _, stopped = outcome(GatewayBackend(a_config(tmp_path), transport=FakeGateway(reply)))

    assert ending(stopped) == UNAVAILABLE
    assert stopped.detail == "gateway_refused"
    assert str(reply.status) in caplog.text


@pytest.mark.parametrize("retry_after", [None, "7"])
@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_server_failure_is_provider_unavailable_and_ignores_retry_after(
    tmp_path, status, retry_after
):
    headers = {} if retry_after is None else {"Retry-After": retry_after}

    _, stopped = outcome(
        GatewayBackend(a_config(tmp_path), transport=FakeGateway(Reply(status, headers)))
    )

    assert ending(stopped) == UNAVAILABLE
    assert (stopped.detail, stopped.retry_after_s) == ("gateway_failed", None)


@pytest.mark.parametrize(
    ("retry_after", "wait"),
    [
        ("7", 7),
        ("0000000007", 7),
        ("86400", MAX_RETRY_AFTER_S),
        ("9" * 5000, MAX_RETRY_AFTER_S),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("-3", None),
        ("soon", None),
        (None, None),
    ],
    ids=[
        "seconds",
        "leading-zeros",
        "capped",
        "digit-limit",
        "http-date",
        "negative",
        "word",
        "none",
    ],
)
def test_busy_carries_the_standard_header_seconds_capped_or_none(tmp_path, retry_after, wait):
    headers = {} if retry_after is None else {"Retry-After": retry_after}

    _, stopped = outcome(
        GatewayBackend(a_config(tmp_path), transport=FakeGateway(Reply(429, headers)))
    )

    assert ending(stopped) == (TurnStopKind.REFUSED, RejectionCode.BUSY)
    assert stopped.retry_after_s == wait


def _wrapped(choice: dict) -> bytes:
    return json.dumps({"choices": [choice]}).encode()


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"[]",
        b'{"choices": []}',
        b'{"choices": "x"}',
        _wrapped({"finish_reason": "stop", "message": {"content": ""}}),
        _wrapped({"finish_reason": "stop", "message": {"content": "  \n"}}),
        _wrapped({"finish_reason": "stop", "message": {"content": None}}),
        _wrapped({"finish_reason": "length", "message": {"content": "cut"}}),
        _wrapped({"finish_reason": "tool_calls", "message": {"content": "x"}}),
        _wrapped({"finish_reason": "stop", "message": {"content": "\ud800"}}),
        _wrapped({"finish_reason": "stop", "message": {"content": "a" * (MAX_EVENT_BYTES + 1)}}),
        b"\xff\xfe",
    ],
    ids=[
        "malformed-json",
        "not-an-object",
        "no-choices",
        "choices-not-a-list",
        "empty-content",
        "blank-content",
        "null-content",
        "length",
        "tool-calls",
        "lone-surrogate",
        "one-byte-past-the-bound",
        "not-utf8",
    ],
)
def test_a_200_that_is_no_complete_text_is_provider_error(tmp_path, body):
    events, stopped = outcome(
        GatewayBackend(a_config(tmp_path), transport=FakeGateway(Reply(200, body=body)))
    )

    assert (events, ending(stopped)) == ([], PROVIDER_ERROR)


def test_a_reply_of_exactly_the_event_bound_is_answered(tmp_path):
    text = "a" * MAX_EVENT_BYTES

    events, stopped = outcome(
        GatewayBackend(a_config(tmp_path), transport=FakeGateway(completion(text)))
    )

    assert stopped is None
    assert events[-1] == FinalEvent(text=text)


def test_a_body_without_end_is_cut_at_the_bound_as_provider_error(tmp_path):
    reply = Reply(200, body=b"x" * (4 * MAX_EVENT_BYTES))

    _, stopped = outcome(GatewayBackend(a_config(tmp_path), transport=FakeGateway(reply)))

    assert ending(stopped) == PROVIDER_ERROR


class _Failing(httpx2.AsyncBaseTransport):
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def handle_async_request(self, request):
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        httpx2.ConnectError("refused"),
        httpx2.ReadError("reset"),
        httpx2.RemoteProtocolError("bad"),
        ConnectionResetError(),
        httpx2.ConnectError("tls handshake failed"),
    ],
)
def test_a_lost_connection_is_provider_unavailable(tmp_path, error):
    _, stopped = outcome(GatewayBackend(a_config(tmp_path), transport=_Failing(error)))

    assert ending(stopped) == UNAVAILABLE


class _Silent(httpx2.AsyncBaseTransport):
    def __init__(self) -> None:
        self.cancelled = False

    async def handle_async_request(self, request):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_a_transport_that_never_answers_ends_at_the_limit(tmp_path):
    silent = _Silent()
    request = a_request().model_copy(update={"max_duration_seconds": 0.05})

    _, stopped = outcome(GatewayBackend(a_config(tmp_path), transport=silent), request)

    assert ending(stopped) == (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
    assert silent.cancelled


class _Trickle(httpx2.AsyncBaseTransport):
    """Answers 200 and then never finishes its body, recording whether its stream was closed."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.started = asyncio.Event()

    async def handle_async_request(self, request):
        transport = self

        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b"{"
                transport.started.set()
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                transport.closed.set()

        return httpx2.Response(200, stream=Body())


def test_cancelling_a_turn_closes_the_response(tmp_path):
    trickle = _Trickle()

    async def scenario() -> bool:
        backend = GatewayBackend(a_config(tmp_path), transport=trickle)
        task = asyncio.create_task(_turn(backend, a_request(), a_person()[0]))
        await trickle.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return trickle.closed.is_set()

    assert asyncio.run(scenario())


def test_a_redirect_is_not_followed_and_a_cookie_is_not_kept(tmp_path):
    gateway = FakeGateway(
        Reply(302, {"Location": "http://elsewhere.test/", "Set-Cookie": "session=1"}),
        completion("ok"),
    )
    backend = GatewayBackend(a_config(tmp_path), transport=gateway)

    _, first = outcome(backend)
    outcome(backend)

    assert ending(first) == UNAVAILABLE
    assert [sent.url.host for sent in gateway.requests] == ["gateway.test", "gateway.test"]
    assert "cookie" not in gateway.requests[1].headers


async def _serve_ok(reader, writer):
    await reader.readuntil(b"\r\n\r\n")
    body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}]})
    head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    writer.write(head.encode() + body.encode())
    await writer.drain()
    writer.close()


def test_the_proxy_environment_is_ignored(tmp_path, monkeypatch):
    # Port 1 refuses; a client that trusted the environment would try it and fail.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")

    async def scenario():
        server = await asyncio.start_server(_serve_ok, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        config = a_config(tmp_path, origin=f"http://127.0.0.1:{port}")
        async with server:
            return await _turn(GatewayBackend(config), a_request(), a_person()[0])

    events, stopped = asyncio.run(scenario())

    assert stopped is None
    assert events[-1] == FinalEvent(text="hi")


def test_debug_logging_writes_the_key_into_no_record(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    backend = GatewayBackend(a_config(tmp_path), transport=FakeGateway(completion("ok")))

    outcome(backend)

    assert KEY not in caplog.text
    assert KEY not in "".join(str(record.__dict__) for record in caplog.records)
