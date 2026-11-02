"""A scripted gateway for an app's real-entry proof: a transport that answers like one.

Pass a ``FakeGateway`` as ``GatewayBackend(config, transport=...)``. Each request takes the
next reply; the last one repeats. ``requests`` holds what the backend sent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2


@dataclass(frozen=True)
class Reply:
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


def completion(text: str, *, finish_reason: str = "stop") -> Reply:
    """A 200 chat completion with one choice."""
    choice = {"finish_reason": finish_reason, "message": {"role": "assistant", "content": text}}
    return Reply(body=json.dumps({"choices": [choice]}).encode())


def tool_call(call_id: str, name: str, arguments: Mapping[str, Any] | str) -> dict[str, Any]:
    """One function call of a model; a string ``arguments`` is sent as written."""
    written = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": written}}


def tool_round(*calls: dict[str, Any]) -> Reply:
    """A 200 chat completion that asks for these calls and ends with ``tool_calls``."""
    message = {"role": "assistant", "content": None, "tool_calls": list(calls)}
    choice = {"finish_reason": "tool_calls", "message": message}
    return Reply(body=json.dumps({"choices": [choice]}).encode())


class FakeGateway(httpx2.AsyncBaseTransport):
    def __init__(self, *replies: Reply) -> None:
        if not replies:
            raise ValueError("a fake gateway needs at least one reply")
        self._replies = list(replies)
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self.requests.append(request)
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        return httpx2.Response(reply.status, headers=dict(reply.headers), content=reply.body)
