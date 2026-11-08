"""Shared arrangement for the MCP person-front suites."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from starlette.applications import Starlette
from starlette.routing import Route

from agent_runtime_client.mcp_server import McpPersonFront
from agent_runtime_client.mcp_wire import MCP_ENDPOINT_PATH, PROTOCOL_VERSION
from agent_runtime_client.tools import ToolDeclaration, ToolEffect

type Headers = list[tuple[bytes, bytes]]


def declaration(name: str, effect: ToolEffect = ToolEffect.READ_ONLY) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        description=f"{name} for tests",
        parameters={"type": "object", "properties": {"note": {"type": "string"}}},
        effect=effect,
    )


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
