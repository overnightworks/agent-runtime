"""What the gateway backend's turn tests share: a person, a config and a streamed gateway."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import ssl
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agents.agent_file import TurnRequest
from agents.backend import TurnPerson, TurnStopKind, TurnStopped
from agents.backend.fake_gateway import FakeGateway, Reply, completion
from agents.backend.gateway import GatewayBackend
from agents.config import AgentConfig
from agents.tools import HandTool, JsonObject, ToolDeclaration, ToolEffect, ToolOutcome
from agents.turn import (
    FailureCode,
    Principal,
    Turn,
    TurnMessage,
    TurnRole,
    TurnUsage,
)
from tests.support.virtual_time import VirtualTimeLoop

API_GROUP = "/apps/example"
GATEWAY_ORIGIN = "http://gateway.test"
GATEWAY_KEY = "stand-in-key-0001"
TURN_BUDGET_SECONDS = 30


class FailingTransport(httpx2.AsyncBaseTransport):
    """Refuses every request with ``error`` (a connection error by default); it counts attempts."""

    def __init__(self, error: Exception | None = None) -> None:
        self.attempts = 0
        self._error = httpx2.ConnectError("refused") if error is None else error

    async def handle_async_request(self, request):
        self.attempts += 1
        raise self._error


class HangingTransport(httpx2.AsyncBaseTransport):
    """Takes the request and never answers, while the clock moves ``seconds_passing`` on."""

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


class Streamed(httpx2.AsyncBaseTransport):
    """Answers with a streamed body, recording whether it was read and whether it was closed.

    After its chunks it hangs, raises ``breaks_with`` or ends; its close raises ``closes_with``.
    """

    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        hangs: bool = False,
        breaks_with: Exception | None = None,
        closes_with: Exception | None = None,
    ) -> None:
        self._chunks = chunks
        self._status = status
        self._headers = headers or {}
        self._hangs = hangs
        self._breaks_with = breaks_with
        self._closes_with = closes_with
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
                if transport._breaks_with is not None:
                    raise transport._breaks_with
                if transport._hangs:
                    await asyncio.Event().wait()

            async def aclose(self) -> None:
                transport.closed.set()
                if transport._closes_with is not None:
                    raise transport._closes_with

        return httpx2.Response(self._status, headers=self._headers, stream=Body())


def a_chunk(
    content: str | None = None, *, tool_calls: list | None = None, finish_reason: str | None = None
) -> dict:
    """One ``chat.completion.chunk`` as the gateway writes it: a null field is absent."""
    delta = {} if content is None else {"content": content}
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    choice = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice]}


def a_fragment(index: int, *, call_id=None, name=None, arguments=None) -> dict:
    function = {} if name is None else {"name": name}
    if arguments is not None:
        function["arguments"] = arguments
    return {"index": index, **({} if call_id is None else {"id": call_id}), "function": function}


def an_event_stream(*events: dict | str, done: bool = True) -> bytes:
    """Events as ``data:`` lines (a string is written as it is), then ``[DONE]`` unless cut."""
    lines = [event if isinstance(event, str) else json.dumps(event) for event in events]
    lines += ["[DONE]"] if done else []
    return "".join(f"data: {line}\n\n" for line in lines).encode()


def a_turn_usage(
    input_tokens: int,
    output_tokens: int,
    *,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    requests: int = 1,
) -> TurnUsage:
    return TurnUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        requests=requests,
    )


def ending(stopped: TurnStopped | None):
    return None if stopped is None else (stopped.kind, stopped.code)


def a_person(groups: frozenset[str] = frozenset({API_GROUP})) -> TurnPerson:
    async def member_of() -> frozenset[str]:
        return groups

    principal = Principal(iss="https://issuer.example", sub="s-1")
    return TurnPerson(principal=principal, role="user", groups=member_of)


def a_gateway_config(directory: Path) -> AgentConfig:
    key_file = directory / "key"
    key_file.write_text(f"{GATEWAY_KEY}\n")
    return AgentConfig.model_validate(
        {
            "backend": "litellm",
            "api_group": API_GROUP,
            "gateway": {"origin": GATEWAY_ORIGIN, "key_file": str(key_file)},
        }
    )


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

    def outcome(self, backend, request=None, person=None, *, leaves_after=None):
        return asyncio.run(
            _turn(backend, request or a_request(), person or self.person()[0], leaves_after)
        )


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


def a_picture_request(**changes) -> TurnRequest:
    turn = Turn(system="Paint it.", messages=[TurnMessage(role=TurnRole.USER, text="a cat")])
    fields = {
        "agent": "painter",
        "turn": turn,
        "model": "image/standin",
        "picture_size": "1024x1024",
    }
    return a_request(**{**fields, **changes})


async def _turn(backend, request, person, leaves_after=None):
    """The events and the stop, if any; the host leaves after ``leaves_after`` events if given."""
    events = []
    try:
        async with contextlib.aclosing(backend.run(request, person)) as stream:
            async for event in stream:
                events.append(event)
                if len(events) == leaves_after:
                    break
    except TurnStopped as stopped:
        return events, stopped
    return events, None


def a_handler_raising(stop: TurnStopped):
    async def stopping(_principal, _arguments):
        raise stop

    return stopping


def a_handler_raising_from_a_nameless_namespace(stop: TurnStopped):
    """A handler whose frame's globals are an empty dict, so they hold no ``__name__``."""

    async def stopping(_principal, _arguments):
        raise stop

    return types.FunctionType(stopping.__code__, {}, closure=stopping.__closure__)


def self_signed_authority(directory: Path, name: str) -> tuple[Path, Path]:
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


def server_context(authority: tuple[Path, Path]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*authority)
    return context


@dataclass
class Shop:
    """The app side of a turn: its hand tools, what they were called with, and the gateway."""

    directory: Path
    seen: list[tuple[str, Principal, JsonObject]] = field(default_factory=list)
    tools: list[HandTool] = field(default_factory=list)

    def add_tool(
        self,
        name: str,
        *,
        effect: ToolEffect = ToolEffect.READ_ONLY,
        answer: Callable[[], Awaitable[ToolOutcome]] | None = None,
        parameters: JsonObject | None = None,
    ) -> HandTool:
        async def handler(principal: Principal, arguments: JsonObject) -> ToolOutcome:
            self.seen.append((name, principal, arguments))
            return await (answer or _noted)()

        tool = HandTool(
            ToolDeclaration(
                name=name, description=f"Use {name}.", parameters=parameters or {}, effect=effect
            ),
            handler,
        )
        self.tools.append(tool)
        return tool

    def run(
        self,
        *replies: Reply,
        max_tool_calls: int = 5,
        selected: tuple[str, ...] | None = None,
        late_after: type | None = None,
        one_call_per_reply: bool = False,
    ) -> tuple[list, TurnStopped | None, FakeGateway]:
        gateway = FakeGateway(*replies)
        chosen = [t for t in self.tools if selected is None or t.declaration.name in selected]
        request = a_tool_request(chosen, max_tool_calls, one_call_per_reply=one_call_per_reply)
        backend = GatewayBackend(self.config(), tools=self.tools, transport=gateway)
        with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
            events, stopped = runner.run(shop_turn(backend, request, late_after))
        return events, stopped, gateway

    def config(self) -> AgentConfig:
        return a_gateway_config(self.directory)


async def _noted() -> ToolOutcome:
    return ToolOutcome("noted", is_error=False)


def a_tool_request(
    tools: list[HandTool], max_tool_calls: int, *, one_call_per_reply: bool = False
) -> TurnRequest:
    return TurnRequest(
        agent="helper",
        turn=Turn(system="Answer briefly.", messages=[TurnMessage(role=TurnRole.USER, text="Hi")]),
        roles=["admin", "user"],
        tools=tuple(tool.declaration for tool in tools),
        model="text/standin",
        max_duration_seconds=TURN_BUDGET_SECONDS,
        max_tool_calls=max_tool_calls if tools else None,
        one_call_per_reply=one_call_per_reply,
    )


async def shop_turn(backend: GatewayBackend, request: TurnRequest, late_after: type | None = None):
    """Run a turn; the app takes `late_after` events so long that the turn's deadline has passed."""
    events = []
    try:
        async with contextlib.aclosing(backend.run(request, a_person())) as stream:
            async for event in stream:
                events.append(event)
                if late_after is not None and isinstance(event, late_after):
                    asyncio.get_running_loop().skew_seconds += request.max_duration_seconds + 1
    except TurnStopped as stopped:
        return events, stopped
    return events, None


def sent(gateway: FakeGateway, index: int) -> dict:
    return json.loads(gateway.requests[index].content)


@pytest.fixture
def shop(tmp_path: Path) -> Shop:
    return Shop(tmp_path)


UNAVAILABLE = (TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE)
