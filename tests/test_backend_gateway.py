"""``GatewayBackend`` against a scripted gateway: the rows of the gateway matrix (M-L1)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agent_runtime_client.backend import MAX_RETRY_AFTER_S, TurnPerson, TurnStopKind, TurnStopped
from agent_runtime_client.backend.fake_gateway import FakeGateway, Reply, completion
from agent_runtime_client.backend.gateway import GatewayBackend, GatewayDetail
from agent_runtime_client.turn import (
    MAX_EVENT_BYTES,
    FailureCode,
    Principal,
    RejectionCode,
    person_fingerprint,
)
from agent_runtime_client.turn_events import AssistantTextEvent, FinalEvent
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import (
    UNAVAILABLE,
    FailingTransport,
    Streamed,
    _turn,
    a_chunk,
    a_request,
    an_event_stream,
    ending,
)

scenario = gateway_turn_support.scenario

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
    "stream_options",
    "max_tokens",
    "tools",
}


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
    assert set(body) == {"model", "messages", "user", "stream"}
    assert body["stream"] is True
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
    refusal = TurnStopped(TurnStopKind.REFUSED, RejectionCode.FORBIDDEN)
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


def test_a_refusal_response_is_closed_after_its_body_is_read(scenario):
    refusal = Streamed((b'{"error":"budget_exceeded"}',), status=422)

    _, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=refusal))

    assert (ending(stopped), stopped.detail) == (UNAVAILABLE, "gateway_refused")
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


def _stopped_with(piece: str) -> bytes:
    return an_event_stream(a_chunk(piece), a_chunk(finish_reason="stop"))


@pytest.mark.parametrize(
    "body",
    [
        an_event_stream("{not json"),
        an_event_stream("[]"),
        an_event_stream({"choices": []}),
        an_event_stream({"choices": "x"}),
        completion("").body,
        completion().body,
        an_event_stream(a_chunk(finish_reason="stop")),
        _stopped_with("\ud800"),
        _stopped_with("a" * (MAX_EVENT_BYTES + 1)),
        b"data: \xff\xfe\n\n",
        an_event_stream("[" * 200_000),
        an_event_stream('{"choices":' + "[" * 200_000),
        an_event_stream("[" * 100_000 + "]" * 100_000),
        b"not json",
        b"",
    ],
    ids=[
        "malformed-json",
        "not-an-object",
        "no-choices",
        "choices-not-a-list",
        "empty-content",
        "no-pieces",
        "null-content",
        "lone-surrogate",
        "one-byte-past-the-bound",
        "not-utf8",
        "nested-open",
        "nested-in-choices",
        "nested-closed",
        "not-an-event-stream",
        "empty-body",
    ],
)
def test_a_200_that_is_no_complete_text_is_provider_error(scenario, body):
    events, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(200, body=body)))
    )

    assert (events, ending(stopped)) == ([], PROVIDER_ERROR)


def test_a_200_body_that_cannot_be_decoded_is_provider_error(scenario):
    undecodable = Streamed((b"this is not gzip",), headers={"Content-Encoding": "gzip"})

    events, stopped = scenario.outcome(GatewayBackend(scenario.config(), transport=undecodable))

    assert (events, ending(stopped)) == ([], PROVIDER_ERROR)
    assert undecodable.closed.is_set()


@pytest.mark.parametrize("unit", ["a", "\\u0061"], ids=["plain", "json_escapes"])
def test_a_reply_of_the_event_bound_is_answered(scenario, unit):
    event = '{"choices":[{"delta":{"content":"' + unit * MAX_EVENT_BYTES + '"}}]}'
    body = an_event_stream(event, a_chunk(finish_reason="stop"))

    events, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(200, body=body)))
    )

    assert stopped is None
    assert events[-1] == FinalEvent(text="a" * MAX_EVENT_BYTES)


def test_a_line_that_never_ends_is_cut_at_the_bound_as_provider_error(scenario):
    endless = Streamed((b"x" * MAX_EVENT_BYTES,) * 16, hangs=True)
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


def test_cancelling_a_turn_closes_the_response(scenario):
    trickle = Streamed((b"data: {",), hangs=True)

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
    transport = Streamed((answer,))

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
    body = an_event_stream(a_chunk("hi"), a_chunk(finish_reason="stop"))
    head = (
        f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    writer.write(head.encode() + body)
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


def test_a_turn_advanced_from_a_new_task_per_event_reads_its_stream_over_a_socket(scenario):
    first_shown = asyncio.Event()

    async def serve_the_rest_once_the_first_piece_showed(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
        writer.write(head + an_event_stream(a_chunk("Hel"), done=False))
        await writer.drain()
        await first_shown.wait()
        writer.write(an_event_stream(a_chunk("lo"), a_chunk(finish_reason="stop")))
        await writer.drain()
        writer.close()

    async def drive():
        server = await asyncio.start_server(
            serve_the_rest_once_the_first_piece_showed, "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        backend = GatewayBackend(scenario.config(origin=f"http://127.0.0.1:{port}"))
        events = []

        async def next_event(stream):
            return await anext(stream, None)

        async with server:
            try:
                turn = backend.run(a_request(max_duration_seconds=5), scenario.person()[0])
                async with contextlib.aclosing(turn) as stream:
                    while (event := await asyncio.create_task(next_event(stream))) is not None:
                        events.append(event)
                        first_shown.set()
            finally:
                # A turn that ends before its first piece still lets the server finish.
                first_shown.set()
        return events

    assert asyncio.run(drive()) == [
        AssistantTextEvent(text="Hel"),
        AssistantTextEvent(text="lo"),
        FinalEvent(text="Hello"),
    ]


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
    caplog.set_level(logging.DEBUG)
    for name in ("httpx2", "httpcore2"):
        caplog.set_level(logging.DEBUG, logger=name)

    events, stopped = _loopback_turn(scenario)

    assert stopped is None
    assert events[-1] == FinalEvent(text="hi")
    assert any(record.name.startswith("httpcore2") for record in caplog.records)
    assert scenario.key not in caplog.text
    assert scenario.key not in "".join(str(record.__dict__) for record in caplog.records)


@pytest.mark.parametrize(
    "name", ["httpx2", "httpcore2", "httpcore2.connection", "httpcore2.http11"]
)
def test_a_transport_logger_the_host_left_unset_is_not_enabled_for_debug_after_a_construction(
    scenario, caplog, name
):
    caplog.set_level(logging.DEBUG)
    for logger in ("httpx2", "httpcore2"):
        caplog.set_level(logging.NOTSET, logger=logger)

    GatewayBackend(scenario.config())

    assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)


@pytest.mark.parametrize("logger", ["httpx2", "httpcore2"])
@pytest.mark.parametrize("level", [logging.DEBUG, logging.ERROR])
def test_a_level_the_host_set_on_a_transport_logger_stands_after_a_construction(
    scenario, caplog, logger, level
):
    caplog.set_level(level, logger=logger)

    GatewayBackend(scenario.config())

    assert logging.getLogger(logger).level == level


def test_a_fake_gateway_without_replies_is_refused():
    with pytest.raises(ValueError, match="at least one reply"):
        FakeGateway()


REPLY_CANARY = "reply-canary-7f3a"
A_PIECE = an_event_stream(a_chunk(REPLY_CANARY), done=False)


def _held_texts(error: BaseException) -> list[str]:
    """Every string a runtime frame of the traceback holds, and the text of each exception.

    The host's own frames are left out: the host holds the events it was given.
    """
    texts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        texts += [str(current), repr(current.args)]
        trace = current.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            if frame.f_globals["__name__"].startswith("agent_runtime_client"):
                texts += [repr(value) for value in frame.f_locals.values()]
            trace = trace.tb_next
        pending += [current.__context__, current.__cause__]
    return texts


TURN_STOPS = {
    "status": (lambda: FakeGateway(Reply(401, body=b'{"error":{"type":"auth"}}')), 30),
    "lost_connection": (lambda: FailingTransport(httpx2.ConnectError("refused")), 30),
    "broken_chunk_header": (
        lambda: Streamed((A_PIECE,), breaks_with=httpx2.RemoteProtocolError("bad chunk")),
        30,
    ),
    "incomplete_round": (lambda: Streamed((A_PIECE,)), 30),
    "malformed_event": (lambda: Streamed((A_PIECE + b"data: {not json\n\n",)), 30),
    "round_cut_short": (
        lambda: Streamed(
            (A_PIECE + b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',)
        ),
        30,
    ),
    "error_event": (
        lambda: Streamed((A_PIECE + b'data: {"error":{"type":"server_error"}}\n\n',)),
        30,
    ),
    "deadline": (_Silent, 0.05),
}


@pytest.mark.parametrize(("transport", "seconds"), TURN_STOPS.values(), ids=TURN_STOPS)
def test_a_stopped_turn_holds_neither_the_key_nor_the_reply_in_any_frame_or_chained_error(
    scenario, transport, seconds
):
    request = a_request().model_copy(update={"max_duration_seconds": seconds})
    backend = GatewayBackend(scenario.config(), transport=transport())

    _, stopped = scenario.outcome(backend, request)

    held = _held_texts(stopped)
    assert [text for text in held if scenario.key in text or REPLY_CANARY in text] == []
