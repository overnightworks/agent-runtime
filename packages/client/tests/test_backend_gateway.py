"""``GatewayBackend`` against a scripted gateway: the rows of the gateway matrix (M-L1)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from tests.support.gateway_turn_support import FailingTransport, ending

from agent_runtime_client.agents import TurnRequest
from agent_runtime_client.backend import MAX_RETRY_AFTER_S, TurnPerson, TurnStopKind, TurnStopped
from agent_runtime_client.backend.fake_gateway import FakeGateway, Reply, completion
from agent_runtime_client.backend.gateway import GatewayBackend, GatewayDetail
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.turn import (
    MAX_EVENT_BYTES,
    FailureCode,
    Principal,
    RejectionCode,
    Turn,
    TurnMessage,
    TurnRole,
    person_fingerprint,
)
from agent_runtime_client.turn_events import AssistantTextEvent, FinalEvent

FINGERPRINT_VECTORS = [
    (
        "https://issuer.example",
        "subject-1",
        "fp-2db44d96ffdef38f9e1cbfe73e9cf248fb66955b894846b34843a2d3a849d764",
    ),
    (
        "https://issuer.example",
        "subjéct-ü-1",
        "fp-4670d575ad432d6edf969bf640bca6a2114230ad288ca5893725c511dd7c94c9",
    ),
]
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


@dataclass(frozen=True)
class Scenario:
    """The example app, key, group and person every test of this file runs as."""

    directory: Path
    key: str = "stand-in-key-0001"
    api_group: str = "/apps/example"
    issuer: str = "https://issuer.example"
    subject: str = "subject-1"

    def config(
        self,
        *,
        origin: str = "http://gateway.test",
        key: bytes | None = None,
        ca_file: Path | None = None,
    ) -> AgentConfig:
        key_file = self.directory / "key"
        key_file.write_bytes(f"{self.key}\n".encode() if key is None else key)
        gateway = {"origin": origin, "key_file": str(key_file)}
        if ca_file is not None:
            gateway["ca_file"] = str(ca_file)
        section = {"backend": "litellm", "gateway": gateway, "api_group": self.api_group}
        return AgentConfig.model_validate(section)

    def person(self, groups=None, *, iss=None, sub=None, role="user"):
        calls = []
        member_of_groups = frozenset({self.api_group}) if groups is None else groups

        async def member_of() -> frozenset[str]:
            calls.append(1)
            if isinstance(member_of_groups, Exception):
                raise member_of_groups
            return member_of_groups

        principal = Principal(iss=iss or self.issuer, sub=sub or self.subject)
        return TurnPerson(principal=principal, role=role, groups=member_of), calls

    def fingerprint(self, iss=None, sub=None) -> str:
        gateway = FakeGateway(completion("ok"))
        person, _ = self.person(iss=iss, sub=sub)
        self.outcome(GatewayBackend(self.config(), transport=gateway), person=person)
        return json.loads(gateway.requests[0].content)["user"]

    def outcome(self, backend, request=None, person=None):
        return asyncio.run(_turn(backend, request or a_request(), person or self.person()[0]))


@pytest.fixture
def scenario(tmp_path) -> Scenario:
    return Scenario(tmp_path)


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
        "max_tool_calls": None,
    }
    return TurnRequest(**{**fields, **changes})


async def _turn(backend, request, person):
    events = []
    try:
        async with contextlib.aclosing(backend.run(request, person)) as stream:
            async for event in stream:
                events.append(event)
    except TurnStopped as stopped:
        return events, stopped
    return events, None


UNAVAILABLE = (TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE)
PROVIDER_ERROR = (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)


def test_a_text_turn_ends_with_the_answer(scenario):
    gateway = FakeGateway(completion("The answer."))

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=gateway))

    assert stopped is None
    assert events == [AssistantTextEvent(text="The answer."), FinalEvent(text="The answer.")]


def test_the_request_is_one_post_with_the_system_prompt_and_the_messages(scenario):
    gateway = FakeGateway(completion("ok"))

    scenario.outcome(GatewayBackend(scenario.config(), transport=gateway))

    (sent,) = gateway.requests
    assert (sent.method, sent.url.path, sent.url.host) == (
        "POST",
        "/v1/chat/completions",
        "gateway.test",
    )
    assert sent.headers["authorization"] == f"Bearer {scenario.key}"
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


def test_no_header_beyond_the_transports_own_defaults_is_sent(scenario):
    gateway = FakeGateway(completion("ok"))

    scenario.outcome(GatewayBackend(scenario.config(), transport=gateway))

    assert {name.lower() for name in gateway.requests[0].headers} <= TRANSPORT_DEFAULT_HEADERS


def test_no_pinned_absent_field_is_sent(scenario):
    gateway = FakeGateway(completion("ok"))

    scenario.outcome(GatewayBackend(scenario.config(), transport=gateway))

    text = gateway.requests[0].content.decode()
    assert not PINNED_ABSENT_BODY_KEYS & set(json.loads(text))
    assert not re.search(r"extra_|x-litellm|anthropic-", text)


@pytest.mark.parametrize(
    ("iss", "sub", "fingerprint"), FINGERPRINT_VECTORS, ids=["ascii", "non-ascii"]
)
def test_the_public_fingerprint_and_the_gateway_user_give_the_documented_vectors(
    scenario, iss, sub, fingerprint
):
    assert scenario.fingerprint(iss, sub) == fingerprint
    assert person_fingerprint(Principal(iss=iss, sub=sub)) == fingerprint


@pytest.mark.parametrize(
    "other",
    [
        lambda iss, sub: (iss + "/", sub),
        lambda iss, sub: (iss, sub + "x"),
        lambda iss, sub: (iss[:-1], iss[-1] + sub),
    ],
    ids=["issuer", "subject", "pair-colliding-under-concatenation"],
)
def test_a_different_principal_gets_a_different_fingerprint(scenario, other):
    assert scenario.fingerprint() != scenario.fingerprint(*other(scenario.issuer, scenario.subject))


def test_a_fingerprint_is_67_characters_and_the_same_for_one_principal(scenario):
    first = scenario.fingerprint()

    assert re.fullmatch(r"fp-[0-9a-f]{64}", first)
    assert first == scenario.fingerprint()


def test_a_role_outside_the_agents_roles_is_forbidden_and_sends_nothing(scenario):
    gateway = FakeGateway(completion("ok"))
    person, calls = scenario.person(role="user")

    _, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=gateway), a_request(roles=["admin"]), person
    )

    assert ending(stopped) == (TurnStopKind.REFUSED, RejectionCode.FORBIDDEN)
    assert (gateway.requests, calls) == ([], [])


def test_a_group_the_person_lacks_is_not_enabled_and_sends_nothing(scenario):
    gateway = FakeGateway(completion("ok"))
    person, calls = scenario.person(frozenset({"/apps/other"}))

    _, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=gateway), person=person
    )

    assert ending(stopped) == (TurnStopKind.REFUSED, RejectionCode.NOT_ENABLED)
    assert (gateway.requests, calls) == ([], [1])


def test_a_refusal_the_groups_lookup_raises_reaches_the_app_unchanged(scenario):
    gateway = FakeGateway(completion("ok"))
    refusal = TurnStopped(TurnStopKind.REFUSED, RejectionCode.UNAVAILABLE)
    person, calls = scenario.person(refusal)

    _, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=gateway), person=person
    )

    assert stopped is refusal
    assert (gateway.requests, calls) == ([], [1])


def test_a_timeout_the_groups_lookup_raises_itself_reaches_the_app_unchanged(scenario):
    gateway = FakeGateway(completion("ok"))
    lookup_timeout = TimeoutError("lookup")
    person, _ = scenario.person(lookup_timeout)
    backend = GatewayBackend(scenario.config(), transport=gateway)

    with pytest.raises(TimeoutError) as raised:
        scenario.outcome(backend, person=person)

    assert (raised.value, gateway.requests) == (lookup_timeout, [])


def test_groups_are_asked_once_per_turn(scenario):
    person, calls = scenario.person()

    backend = GatewayBackend(scenario.config(), transport=FakeGateway(completion("ok")))
    scenario.outcome(backend, person=person)

    assert calls == [1]


def test_a_generator_never_iterated_opens_nothing_and_reads_no_key(scenario):
    config = scenario.config()
    config.gateway.key_file.unlink()
    gateway = FakeGateway(completion("ok"))

    backend = GatewayBackend(config, transport=gateway)
    backend.run(a_request(), scenario.person()[0])

    assert gateway.requests == []


@pytest.mark.parametrize(
    ("content", "secret"),
    [
        (None, None),
        (b"x" * 100_000, "x" * 64),
        (b"stand-in-key-0001\nsecond\n", "stand-in-key-0001"),
        (b"first\nsecond\n", "second"),
        (b"", None),
        (b"has space\n", "has space"),
        ("kéy\n".encode(), "kéy"),
    ],
    ids=["missing", "oversized", "key-then-line", "two-lines", "empty", "space", "non-ascii"],
)
def test_an_unusable_key_file_is_provider_unavailable_and_sends_nothing(
    scenario, caplog, content, secret
):
    caplog.set_level(logging.DEBUG)
    config = scenario.config(key=content or b"")
    if content is None:
        config.gateway.key_file.unlink()
    gateway = FakeGateway(completion("ok"))

    _, stopped = scenario.outcome(GatewayBackend(config, transport=gateway))

    assert ending(stopped) == UNAVAILABLE
    assert gateway.requests == []
    everything_it_carries = " ".join(
        [
            str(stopped),
            repr(stopped),
            str(stopped.detail),
            repr(stopped.__context__),
            repr(stopped.__cause__),
            caplog.text,
        ]
    )
    assert secret is None or secret not in everything_it_carries


def test_a_key_with_any_prefix_is_sent_as_it_is(scenario):
    gateway = FakeGateway(completion("ok"))

    scenario.outcome(GatewayBackend(scenario.config(key=b"sk-anything/+=\n"), transport=gateway))

    assert gateway.requests[0].headers["authorization"] == "Bearer sk-anything/+="


@pytest.mark.parametrize(
    "reply",
    [Reply(401), Reply(403), Reply(422, body=b'{"error":"budget_exceeded"}'), Reply(404)],
)
def test_a_refusal_status_is_provider_unavailable_as_gateway_refused(scenario, reply, caplog):
    caplog.set_level(logging.INFO)

    _, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=FakeGateway(reply)))

    assert ending(stopped) == UNAVAILABLE
    assert stopped.detail == "gateway_refused"
    assert str(reply.status) in caplog.text


def test_a_refusal_body_is_never_read_and_the_response_is_closed(scenario):
    refusal = _Streamed((b'{"error":"budget_exceeded"}',), status=422)

    _, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=refusal))

    assert (ending(stopped), stopped.detail) == (UNAVAILABLE, "gateway_refused")
    assert not refusal.read
    assert refusal.closed.is_set()


@pytest.mark.parametrize("retry_after", [None, "7"])
@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_server_failure_is_provider_unavailable_and_ignores_retry_after(
    scenario, status, retry_after
):
    headers = {} if retry_after is None else {"Retry-After": retry_after}

    _, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(status, headers)))
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
def test_busy_carries_the_standard_header_seconds_capped_or_none(scenario, retry_after, wait):
    headers = {} if retry_after is None else {"Retry-After": retry_after}

    _, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(429, headers)))
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
        b"[" * 200_000,
        b'{"choices":' + b"[" * 200_000,
        b"[" * 100_000 + b"]" * 100_000,
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
        "nested-open",
        "nested-in-choices",
        "nested-closed",
    ],
)
def test_a_200_that_is_no_complete_text_is_provider_error(scenario, body):
    events, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(200, body=body)))
    )

    assert (events, ending(stopped)) == ([], PROVIDER_ERROR)


def test_a_200_body_that_cannot_be_decoded_is_provider_error(scenario):
    undecodable = _Streamed((b"this is not gzip",), headers={"Content-Encoding": "gzip"})

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=undecodable))

    assert (events, ending(stopped)) == ([], PROVIDER_ERROR)
    assert undecodable.closed.is_set()


@pytest.mark.parametrize("unit", ["a", "\\u0061"], ids=["plain", "json_escapes"])
def test_a_reply_of_the_event_bound_is_answered(scenario, unit):
    body = (
        '{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"'
        + unit * MAX_EVENT_BYTES
        + '"}}],"usage":{"total_tokens":1}}'
    ).encode()

    events, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(200, body=body)))
    )

    assert stopped is None
    assert events[-1] == FinalEvent(text="a" * MAX_EVENT_BYTES)


def test_a_body_that_never_ends_is_cut_at_the_bound_as_provider_error(scenario):
    endless = _Streamed((b"x" * MAX_EVENT_BYTES,) * 16, hangs=True)
    request = a_request(max_duration_seconds=5)

    _, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=endless), request)

    assert ending(stopped) == PROVIDER_ERROR
    assert endless.closed.is_set()


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
def test_a_lost_connection_is_provider_unavailable(scenario, error):
    backend = GatewayBackend(scenario.config(), transport=FailingTransport(error))

    _, stopped = scenario.outcome(backend)

    assert ending(stopped) == UNAVAILABLE


def test_an_origin_the_http_library_refuses_is_provider_unavailable(scenario):
    backend = GatewayBackend(scenario.config(origin="http://999.999.999.999"))

    _, stopped = scenario.outcome(backend)

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


def test_a_transport_that_never_answers_ends_at_the_limit(scenario):
    silent = _Silent()
    request = a_request().model_copy(update={"max_duration_seconds": 0.05})

    _, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=silent), request)

    assert ending(stopped) == (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
    assert silent.cancelled


def test_a_groups_lookup_that_never_answers_ends_at_the_limit(scenario):
    async def never() -> frozenset[str]:
        await asyncio.Event().wait()
        return frozenset()

    person = TurnPerson(principal=Principal(iss="i", sub="s"), role="user", groups=never)
    gateway = FakeGateway(completion("ok"))
    request = a_request().model_copy(update={"max_duration_seconds": 0.05})
    backend = GatewayBackend(scenario.config(), transport=gateway)

    # A turn that ignores its limit would hang; the outer bound turns that into a failure.
    _, stopped = asyncio.run(asyncio.wait_for(_turn(backend, request, person), timeout=5))

    assert ending(stopped) == (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
    assert gateway.requests == []


class _Streamed(httpx2.AsyncBaseTransport):
    """Answers with a streamed body, recording whether it was read and whether it was closed."""

    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        hangs: bool = False,
    ) -> None:
        self._chunks = chunks
        self._status = status
        self._headers = headers or {}
        self._hangs = hangs
        self.read = False
        self.closed = asyncio.Event()
        self.started = asyncio.Event()

    async def handle_async_request(self, request):
        transport = self

        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                transport.read = True
                for chunk in transport._chunks:
                    yield chunk
                transport.started.set()
                if transport._hangs:
                    await asyncio.Event().wait()

            async def aclose(self) -> None:
                transport.closed.set()

        return httpx2.Response(self._status, headers=self._headers, stream=Body())


def test_cancelling_a_turn_closes_the_response(scenario):
    trickle = _Streamed((b"{",), hangs=True)

    async def drive() -> bool:
        backend = GatewayBackend(scenario.config(), transport=trickle)
        task = asyncio.create_task(_turn(backend, a_request(), scenario.person()[0]))
        await trickle.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return trickle.closed.is_set()

    assert asyncio.run(drive())


def test_closing_a_suspended_turn_leaves_the_response_closed_and_yields_nothing_more(scenario):
    answer = completion("The answer.").body
    transport = _Streamed((answer,))

    async def scenario_run():
        backend = GatewayBackend(scenario.config(), transport=transport)
        stream = backend.run(a_request(), scenario.person()[0])
        first = await anext(stream)
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return first, transport.closed.is_set()

    assert asyncio.run(scenario_run()) == (AssistantTextEvent(text="The answer."), True)


def test_a_redirect_is_not_followed_and_a_cookie_is_not_kept(scenario):
    gateway = FakeGateway(
        Reply(302, {"Location": "http://elsewhere.test/", "Set-Cookie": "session=1"}),
        completion("ok"),
    )
    backend = GatewayBackend(scenario.config(), transport=gateway)

    _, first = scenario.outcome(backend)
    scenario.outcome(backend)

    assert (first.kind, first.code, first.detail) == (*UNAVAILABLE, GatewayDetail.GATEWAY_FAILED)
    assert [sent.url.host for sent in gateway.requests] == ["gateway.test", "gateway.test"]
    assert "cookie" not in gateway.requests[1].headers


async def _serve_ok(reader, writer):
    await reader.readuntil(b"\r\n\r\n")
    body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}]})
    head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    writer.write(head.encode() + body.encode())
    await writer.drain()
    writer.close()


def _loopback_turn(scenario, *, server_context=None, ca_file=None):
    async def drive():
        server = await asyncio.start_server(_serve_ok, "127.0.0.1", 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        scheme = "http" if server_context is None else "https"
        config = scenario.config(origin=f"{scheme}://127.0.0.1:{port}", ca_file=ca_file)
        async with server:
            return await _turn(GatewayBackend(config), a_request(), scenario.person()[0])

    return asyncio.run(drive())


def test_the_proxy_environment_is_ignored(scenario, monkeypatch):
    # Port 1 refuses; a client that trusted the environment would try it and fail.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")

    events, stopped = _loopback_turn(scenario)

    assert stopped is None
    assert events[-1] == FinalEvent(text="hi")


def _self_signed_authority(directory: Path, name: str) -> tuple[Path, Path]:
    """A certificate for 127.0.0.1 that is its own authority: (certificate file, key file)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_file, key_file = directory / f"{name}.pem", directory / f"{name}.key"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_file, key_file


def _server_context(authority: tuple[Path, Path]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*authority)
    return context


def test_https_is_trusted_through_the_configured_ca_file_alone(scenario):
    served = _self_signed_authority(scenario.directory, "served")
    other = _self_signed_authority(scenario.directory, "other")
    context = _server_context(served)

    trusted, trusted_stop = _loopback_turn(scenario, server_context=context, ca_file=served[0])
    distrusted, distrusted_stop = _loopback_turn(scenario, server_context=context, ca_file=other[0])

    assert (trusted_stop, trusted[-1]) == (None, FinalEvent(text="hi"))
    assert (distrusted, ending(distrusted_stop)) == ([], UNAVAILABLE)


def test_an_unusable_ca_file_is_provider_unavailable(scenario):
    served = _self_signed_authority(scenario.directory, "served")
    unusable = scenario.directory / "not-a-ca.pem"
    unusable.write_text("not a certificate")

    events, stopped = _loopback_turn(
        scenario, server_context=_server_context(served), ca_file=unusable
    )

    assert (events, ending(stopped)) == ([], UNAVAILABLE)


def test_debug_logging_writes_the_key_into_no_record(scenario, caplog):
    for name in ("httpx2", "httpcore2"):
        caplog.set_level(logging.DEBUG, logger=name)
    caplog.set_level(logging.DEBUG)

    events, stopped = _loopback_turn(scenario)

    assert stopped is None
    assert events[-1] == FinalEvent(text="hi")
    assert scenario.key not in caplog.text
    assert scenario.key not in "".join(str(record.__dict__) for record in caplog.records)


@pytest.mark.parametrize(
    "name", ["httpx2", "httpcore2", "httpcore2.connection", "httpcore2.http11"]
)
def test_the_transport_loggers_that_could_carry_the_key_are_not_enabled_for_debug(
    scenario, caplog, name
):
    for logger in ("httpx2", "httpcore2"):
        caplog.set_level(logging.DEBUG, logger=logger)
    GatewayBackend(scenario.config())
    assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)


def test_a_fake_gateway_without_replies_is_refused():
    with pytest.raises(ValueError, match="at least one reply"):
        FakeGateway()
