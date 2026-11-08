"""Shared arrangement for the MCP person-front suites."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from starlette.applications import Starlette
from starlette.routing import Route

from agent_runtime_client.mcp_server import InMemoryMemberChanges, McpPersonFront, wait_tool
from agent_runtime_client.mcp_wire import MCP_ENDPOINT_PATH, PROTOCOL_VERSION
from agent_runtime_client.tools import Principal, ToolDeclaration, ToolEffect
from agent_runtime_client.wait import (
    Invitation,
    PlaceRef,
    Standing,
    StandingState,
    WaitAnswer,
)

ISSUER = "https://issuer.test"
type Headers = list[tuple[bytes, bytes]]


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


@dataclass
class Places:
    """The host's participation state, the one source of truth the wait tool reads.

    ``before_taking`` and ``after_taking`` run inside a read, just before and just after the
    state is taken, so a test can change the state at either moment.
    """

    standings: dict[Principal, tuple[Standing, ...]] = field(default_factory=dict)
    before_taking: Callable[[], None] = lambda: None
    after_taking: Callable[[], None] = lambda: None
    failure: Exception | None = None
    read_seconds: float = 0

    async def read(self, recipient: Principal) -> tuple[Standing, ...]:
        await asyncio.sleep(self.read_seconds)
        if self.failure is not None:
            raise self.failure
        self.before_taking()
        current = self.standings.get(recipient, ())
        self.after_taking()
        return current


@dataclass(frozen=True)
class ToolResult:
    """One ``tools/call`` result as the wire carried it, and how long the call took."""

    text: str
    is_error: bool
    seconds: float

    def answer(self) -> WaitAnswer:
        return WaitAnswer.model_validate_json(self.text)


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
) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if not notification:
        message["id"] = 1
    message["params"] = with_envelope(params) if enveloped else dict(params or {})
    return json.dumps(message).encode()


def call_params(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def tool_call_body(name: str, arguments: dict[str, Any]) -> bytes:
    return rpc_body("tools/call", call_params(name, **arguments))


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


def result_of(sent: list[dict[str, Any]]) -> Any:
    return json.loads(b"".join(message.get("body", b"") for message in sent))["result"]


async def call_tool(
    front: McpPersonFront, member: Principal, name: str, arguments: dict[str, Any]
) -> ToolResult:
    started = now()
    result = result_of(await post(front, member, tool_call_body(name, arguments)))
    [content] = result["content"]
    return ToolResult(content["text"], result["isError"], now() - started)


@dataclass
class WaitScenario:
    """One app serving the wait tool: its places, its change signal and its person front."""

    places: Places = field(default_factory=Places)
    changes: InMemoryMemberChanges = field(default_factory=InMemoryMemberChanges)

    @property
    def front(self) -> McpPersonFront:
        return McpPersonFront(
            [wait_tool(self.changes, self.places.read)], resolve_person=resolve_member
        )

    async def wait(self, member: Principal, **arguments: Any) -> ToolResult:
        front = self.front
        async with front.running():
            return await call_tool(front, member, "wait", arguments)

    async def post(
        self, member: Principal, body: bytes, sent: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        front = self.front
        async with front.running():
            return await post(front, member, body, sent)

    def change(self, member: Principal, *standings: Standing) -> None:
        """The app's own update: the state is visible first, then the change is signalled."""
        self.places.standings[member] = standings
        self.changes.changed(member)
