"""Shared arrangement for the MCP person-front suites."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_runtime_client.mcp_server import InMemoryMemberChanges, McpPersonFront, wait_tool
from agent_runtime_client.mcp_wire import LATEST_PROTOCOL_VERSION, MCP_ENDPOINT_PATH
from agent_runtime_client.tools import Principal, ToolDeclaration, ToolEffect
from agent_runtime_client.wait import (
    Invitation,
    PlaceRef,
    Standing,
    StandingState,
    WaitAnswer,
)

ISSUER = "https://issuer.test"


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


def tool_call_body(name: str, arguments: dict[str, Any]) -> bytes:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    return json.dumps(message | {"params": {"name": name, "arguments": arguments}}).encode()


def http_scope(headers: list[tuple[bytes, bytes]], *, method: str = "POST") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": MCP_ENDPOINT_PATH,
        "query_string": b"",
        "headers": headers,
    }


def member_scope(member: Principal) -> dict[str, Any]:
    return http_scope(
        [
            (b"x-iss", member.iss.encode()),
            (b"x-sub", member.sub.encode()),
            (b"mcp-protocol-version", str(LATEST_PROTOCOL_VERSION).encode()),
        ]
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
    return await drive(front, member_scope(member), body, sent)


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
        return await call_tool(self.front, member, "wait", arguments)

    def change(self, member: Principal, *standings: Standing) -> None:
        """The app's own update: the state is visible first, then the change is signalled."""
        self.places.standings[member] = standings
        self.changes.changed(member)
