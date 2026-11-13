"""The host side of the person-front suites: a resolver, recording tools and a one-shot POST."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from mcp_types import CallToolResult

from agents.mcp_server import McpPersonFront, PersonRequest, PersonTool
from agents.mcp_wire import PROTOCOL_VERSION
from agents.tools import Principal, ToolEffect, ToolOutcome
from agents.turn import JsonObject
from tests.support.mcp_server_support import (
    Headers,
    declaration,
    drive,
    http_scope,
    routing_headers,
    sdk_client,
)


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Headers
    body: bytes

    def header(self, name: str) -> list[str]:
        return [v.decode() for k, v in self.headers if k == name.encode()]

    def json(self) -> Any:
        return json.loads(self.body)


def reply_of(sent: list[dict[str, Any]]) -> Reply:
    [start] = [m for m in sent if m["type"] == "http.response.start"]
    return Reply(start["status"], start["headers"], b"".join(m.get("body", b"") for m in sent))


async def drive_scope(front: McpPersonFront, scope: dict[str, Any], body: bytes) -> Reply:
    async with front.running():
        return reply_of(await drive(front, scope, body))


@dataclass
class Person:
    """The host side: a resolver that reads the caller off a header, and recording tools."""

    resolved: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, PersonRequest, JsonObject]] = field(default_factory=list)
    outcome: ToolOutcome = ToolOutcome("done", is_error=False)
    failure: Exception | None = None

    def resolve(self, scope: dict[str, Any]) -> Principal:
        self.resolved.append(scope)
        headers = dict(scope["headers"])
        if b"x-refused" in headers:
            raise LookupError("the gate admitted nobody")
        return Principal(iss="https://issuer.test", sub=headers.get(b"x-person", b"alice").decode())

    def tool(self, name: str, effect: ToolEffect = ToolEffect.READ_ONLY) -> PersonTool:
        async def handler(request: PersonRequest, arguments: JsonObject) -> ToolOutcome:
            await asyncio.sleep(0)
            self.calls.append((name, request, arguments))
            if self.failure is not None:
                raise self.failure
            return self.outcome

        return PersonTool(declaration(name, effect), handler)


def headers_of(
    body: bytes, *extra: tuple[bytes, bytes], version: str | None = PROTOCOL_VERSION
) -> Headers:
    return [(b"host", b"notes.internal"), *extra, *routing_headers(body, version)]


def post(front: McpPersonFront, body: bytes, *extra: tuple[bytes, bytes], **options: Any) -> Reply:
    version = options.pop("version", PROTOCOL_VERSION)
    scope = http_scope(headers_of(body, *extra, version=version), **options)
    return asyncio.run(drive_scope(front, scope, body))


async def call_read_note(front: McpPersonFront) -> CallToolResult:
    async with sdk_client(front) as client:
        return await client.call_tool("read_note", {})
