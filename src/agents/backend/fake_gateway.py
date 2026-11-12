"""A scripted gateway for an app's real-entry proof: a transport that answers like one.

Pass a ``FakeGateway`` as ``GatewayBackend(config, transport=...)``. Each request takes the
next reply; the last one repeats. ``requests`` holds what the backend sent.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx2
from pydantic import JsonValue

from agents.tools import JsonObject


@dataclass(frozen=True)
class Reply:
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


def _event_stream(*chunks: JsonObject, finish_reason: str) -> Reply:
    finish: JsonObject = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    chunks += (finish,)
    events = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks] + ["data: [DONE]\n\n"]
    return Reply(headers={"content-type": "text/event-stream"}, body="".join(events).encode())


def _delta(**delta: JsonValue) -> JsonObject:
    return {"choices": [{"index": 0, "delta": delta}]}


def completion(*pieces: str, finish_reason: str = "stop") -> Reply:
    """A 200 event stream: one content chunk per piece, the finish chunk and ``[DONE]``.

    ``completion()`` sends no content chunk, the same round as ``completion("")``: no text.
    """
    chunks = (_delta(content=piece) for piece in pieces)
    return _event_stream(*chunks, finish_reason=finish_reason)


def tool_call(call_id: str, name: str, arguments: JsonObject | str) -> JsonObject:
    """One function call of a model; a string ``arguments`` is sent as written."""
    written = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": written}}


def tool_round(*calls: JsonObject, content: str | None = None) -> Reply:
    """A 200 event stream that asks for these calls.

    An optional content chunk, one fragment per call with its whole arguments, the ``tool_calls``
    finish chunk and ``[DONE]``.
    """
    chunks = [] if content is None else [_delta(content=content)]
    chunks += [_delta(tool_calls=[{"index": index, **call}]) for index, call in enumerate(calls)]
    return _event_stream(*chunks, finish_reason="tool_calls")


def picture(content: bytes) -> Reply:
    """A 200 image generation that returns these bytes as the one entry's ``b64_json``."""
    entry = {"b64_json": base64.b64encode(content).decode("ascii")}
    return Reply(body=json.dumps({"data": [entry]}).encode())


def model_list(*ids: str) -> Reply:
    """A 200 model list that offers these ids."""
    return Reply(body=json.dumps({"data": [{"id": model_id} for model_id in ids]}).encode())


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
