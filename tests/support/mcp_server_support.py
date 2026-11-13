"""Shared arrangement for the MCP person-front suites."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from starlette.applications import Starlette
from starlette.routing import Route

from agents.mcp_server import McpPersonFront, PersonRequest, PersonTool
from agents.mcp_wire import MCP_ENDPOINT_PATH, PROTOCOL_VERSION
from agents.member import (
    Invitation,
    MemberStanding,
    PlaceRef,
    PlaceView,
    PlaceWindow,
    Standing,
    StandingState,
    place_view_uri,
    place_window_uri,
)
from agents.tools import Principal, ToolDeclaration, ToolEffect, ToolOutcome
from agents.turn import JsonObject

ISSUER = "https://issuer.test"

type Headers = list[tuple[bytes, bytes]]


def window_uri(place_id: str) -> str:
    return place_window_uri(PlaceRef(kind="call", id=place_id))


def view_uri(place_id: str) -> str:
    return place_view_uri(PlaceRef(kind="call", id=place_id))


def a_view(revision: int = 1) -> PlaceView:
    """A view whose state nests objects, arrays and every JSON scalar."""
    state = {
        "items": [{"label": "ä", "count": 2}, [None, True, 1, 1.0, 1.5]],
        "open": True,
        "note": None,
    }
    return PlaceView(revision=revision, awaits_you=False, state=state)


def an_empty_record() -> MemberStanding:
    return MemberStanding(standings=())


def readable_uris(*uris: str) -> dict[str, MemberStanding]:
    return dict.fromkeys(uris, an_empty_record())


def declaration(name: str, effect: ToolEffect = ToolEffect.READ_ONLY) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        description=f"{name} for tests",
        parameters={"type": "object", "properties": {"note": {"type": "string"}}},
        effect=effect,
    )


def now() -> float:
    return asyncio.get_running_loop().time()


def a_member(sub: str = "alice", iss: str = ISSUER) -> Principal:
    return Principal(iss=iss, sub=sub)


def resolve_member(scope: dict[str, Any]) -> Principal:
    """The host's gate stand-in: the member travels in two test headers."""
    headers = dict(scope["headers"])
    return a_member(headers[b"x-sub"].decode(), headers[b"x-iss"].decode())


def a_standing(place_id: str, state: StandingState) -> Standing:
    invitation = None
    if state is StandingState.INVITED:
        expires_at = datetime(2026, 11, 8, 12, tzinfo=UTC)
        invitation = Invitation(id=f"invitation-{place_id}", epoch=0, expires_at=expires_at)
    return Standing(place=PlaceRef(kind="call", id=place_id), state=state, invitation=invitation)


def a_person_tool() -> PersonTool:
    async def answer_note(_request: PersonRequest, _arguments: JsonObject) -> ToolOutcome:
        await asyncio.sleep(0)
        return ToolOutcome("a note")

    return PersonTool(declaration("read_note"), answer_note)


@dataclass
class MemberApp:
    """The host's truth behind the resources port: per member, what is readable by URI.

    ``asked`` records every principal and URI the port was asked about; ``unusable`` is what both
    methods return instead of their answer when set, a port that breaks its contract. ``current``
    takes ``current_seconds``, records its principal in ``current_asked`` and runs
    ``before_current`` before it answers, so a test can change the state at that moment.
    """

    readable: dict[Principal, dict[str, MemberStanding | PlaceWindow | PlaceView]] = field(
        default_factory=dict
    )
    failure: Exception | None = None
    unusable: Any = None
    asked: list[tuple[Principal, str]] = field(default_factory=list)
    current_seconds: float = 0
    current_asked: list[Principal] = field(default_factory=list)
    before_current: Callable[[], None] = lambda: None

    async def current(self, principal: Principal) -> Collection[str]:
        await asyncio.sleep(self.current_seconds)
        self.current_asked.append(principal)
        self.before_current()
        if self.failure is not None:
            raise self.failure
        if self.unusable is not None:
            return self.unusable
        return sorted(self.readable.get(principal, {}), reverse=True)

    async def read(
        self, principal: Principal, uri: str
    ) -> MemberStanding | PlaceWindow | PlaceView | None:
        await asyncio.sleep(0)
        self.asked.append((principal, uri))
        if self.failure is not None:
            raise self.failure
        if self.unusable is not None:
            return self.unusable
        return self.readable.get(principal, {}).get(uri)


@dataclass
class ToolOffer:
    """The host's offered-tools port: per member, the names offered now.

    ``asked`` records every principal it was asked about; ``failure`` is raised and ``unusable``
    is answered instead of the names when set, a port that breaks its contract.
    """

    offered: dict[Principal, Collection[str]] = field(default_factory=dict)
    failure: Exception | None = None
    unusable: Any = None
    asked: list[Principal] = field(default_factory=list)

    async def __call__(self, principal: Principal) -> Collection[str]:
        await asyncio.sleep(0)
        self.asked.append(principal)
        if self.failure is not None:
            raise self.failure
        if self.unusable is not None:
            return self.unusable
        return self.offered.get(principal, ())


def with_envelope(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``params`` with the per-request ``_meta`` every revision 2026-07-28 request carries."""
    meta = {PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION, CLIENT_CAPABILITIES_META_KEY: {}}
    return {**(params or {}), "_meta": meta}


def rpc_body(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    notification: bool = False,
    enveloped: bool = True,
    request_id: int | str = 1,
) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if not notification:
        message["id"] = request_id
    message["params"] = with_envelope(params) if enveloped else dict(params or {})
    return json.dumps(message).encode()


def call_params(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def routing_headers(body: bytes, version: str | None = PROTOCOL_VERSION) -> Headers:
    """The headers a 2026-07-28 client derives from its body, with the version it declares.

    A body that is no JSON-RPC request object gets the version and ``Accept`` alone.
    """
    fields: Headers = [
        (b"accept", b"application/json, text/event-stream"),
        (b"content-type", b"application/json"),
    ]
    if version is not None:
        fields.append((b"mcp-protocol-version", version.encode()))
    try:
        document = json.loads(body)
    except ValueError:
        return fields
    if not isinstance(document, dict):
        return fields
    fields.append((b"mcp-method", str(document.get("method")).encode()))
    if document.get("method") == "tools/call":
        fields.append((b"mcp-name", str(document["params"]["name"]).encode()))
    if document.get("method") == "resources/read":
        fields.append((b"mcp-name", str(document["params"]["uri"]).encode()))
    return fields


def http_scope(headers: Headers, *, method: str = "POST") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": MCP_ENDPOINT_PATH,
        "query_string": b"",
        "headers": headers,
    }


def member_scope(member: Principal, body: bytes) -> dict[str, Any]:
    return http_scope(
        [(b"x-iss", member.iss.encode()), (b"x-sub", member.sub.encode()), *routing_headers(body)]
    )


async def drive(
    front: McpPersonFront,
    scope: dict[str, Any],
    body: bytes,
    sent: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Sends ``body`` to the front under ``scope`` and returns every ASGI message it sent.

    A caller that may cancel the drive passes the list ``sent`` it owns, so messages sent before
    the cancellation stay visible.
    """
    sent = [] if sent is None else sent

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await front(scope, receive, send)
    return sent


async def post(
    front: McpPersonFront,
    member: Principal,
    body: bytes,
    sent: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return await drive(front, member_scope(member, body), body, sent)


def served_app(front: McpPersonFront) -> Starlette:
    """A host's own application: the front on its exact route, entered in the host's lifespan."""
    return Starlette(routes=[Route(MCP_ENDPOINT_PATH, front)], lifespan=lambda _: front.running())


@asynccontextmanager
async def sdk_client(front: McpPersonFront) -> AsyncIterator[Client]:
    """The SDK's own ``Client``, discovering the revision through real scopes of a running front."""
    url = f"http://notes.internal{MCP_ENDPOINT_PATH}"
    async with (
        front.running(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(front)) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        yield client


async def settled() -> None:
    """One virtual second: every task that can run has run, and no real time passed."""
    await asyncio.sleep(1)


@dataclass
class ListenConnection:
    """One client's stream as hand-driven ASGI messages, so a frame shows the moment it is written.

    The write of the body numbered ``stall_at`` (from 0) waits for ``release``; ``disconnect``
    ends the connection the way a client leaving does.
    """

    front: McpPersonFront
    scope: dict[str, Any]
    body: bytes
    stall_at: int | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    returned_after: float | None = None
    bodies_written: int = 0
    released: asyncio.Event = field(default_factory=asyncio.Event)
    left: asyncio.Event = field(default_factory=asyncio.Event)
    request_delivered: bool = False

    def release(self) -> None:
        self.released.set()

    def disconnect(self) -> None:
        self.left.set()

    async def receive(self) -> dict[str, Any]:
        if not self.request_delivered:
            self.request_delivered = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.left.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            if self.bodies_written == self.stall_at:
                await self.released.wait()
            self.bodies_written += 1
        self.sent.append(message)

    async def run(self) -> None:
        started = now()
        await self.front(self.scope, self.receive, self.send)
        self.returned_after = now() - started

    @property
    def status(self) -> int:
        return self.sent[0]["status"]

    @property
    def body_bytes(self) -> bytes:
        return b"".join(message.get("body", b"") for message in self.sent)

    @property
    def frames(self) -> list[dict[str, Any]]:
        """Every JSON-RPC message the stream wrote, in order; a ping is no message."""
        data_prefix = "data: "
        lines = self.body_bytes.decode().split("\r\n")
        return [
            json.loads(line.removeprefix(data_prefix))
            for line in lines
            if line.startswith(data_prefix)
        ]

    @property
    def updated_uris(self) -> list[str]:
        return [
            frame["params"]["uri"]
            for frame in self.frames
            if frame.get("method") == "notifications/resources/updated"
        ]


def listen_connection(
    front: McpPersonFront,
    member: Principal,
    uris: Sequence[str],
    *,
    request_id: int | str = 1,
    stall_at: int | None = None,
    flags: Mapping[str, bool] | None = None,
    bearer: str | None = None,
) -> ListenConnection:
    filter_ = {"resourceSubscriptions": list(uris), **(flags or {})}
    body = rpc_body("subscriptions/listen", {"notifications": filter_}, request_id=request_id)
    scope = member_scope(member, body)
    if bearer is not None:
        scope["headers"].append((b"authorization", b"Bearer " + bearer.encode("iso-8859-1")))
    return ListenConnection(front, scope, body, stall_at=stall_at)


@asynccontextmanager
async def open_stream(connection: ListenConnection) -> AsyncIterator[ListenConnection]:
    """The connection running until the block ends; leaving the block is the client leaving."""
    async with anyio.create_task_group() as group:
        group.start_soon(connection.run)
        await settled()
        yield connection
        connection.disconnect()


@asynccontextmanager
async def clock_held() -> AsyncIterator[Callable[[], None]]:
    """A virtual loop's clock stands still until the yielded function runs.

    A busy loop never reports idle, so real socket bytes still in flight do not look like a
    skipped sleep; the frames a test acts on arrive at the moment the clock says they were sent.
    """
    released = asyncio.Event()

    async def keep_busy() -> None:
        while not released.is_set():
            await asyncio.sleep(0)

    holding = asyncio.create_task(keep_busy())
    yield released.set
    released.set()
    await holding


def loopback_server(front: McpPersonFront) -> uvicorn.Server:
    """A real uvicorn for ``loopback_client``, for a test that stops it while a client streams."""
    return uvicorn.Server(uvicorn.Config(served_app(front), log_config=None))


@asynccontextmanager
async def loopback_client(
    server: uvicorn.Server, member: Principal, *, bearer: str | None = None
) -> AsyncIterator[Client]:
    """The SDK's ``Client`` against the server on a pre-bound loopback socket in this loop."""
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        bound.listen()
        port = bound.getsockname()[1]
        serving = asyncio.create_task(server.serve(sockets=[bound]))
        headers = {"x-iss": member.iss, "x-sub": member.sub}
        if bearer is not None:
            headers["authorization"] = f"Bearer {bearer}"
        url = f"http://127.0.0.1:{port}{MCP_ENDPOINT_PATH}"
        timeout = httpx2.Timeout(30, read=None)
        async with (
            httpx2.AsyncClient(headers=headers, timeout=timeout, trust_env=False) as http,
            Client(streamable_http_client(url, http_client=http)) as client,
        ):
            yield client
        server.should_exit = True
        await serving
