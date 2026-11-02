"""Builders and fakes for the client's ``ServiceBackend`` suites."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import SecretStr
from tests.support.service_support import run_body

from agent_runtime_client.backend import (
    AgentBackend,
    Asker,
    RunRegistrationRefused,
    RunTokens,
    TurnEvent,
    TurnStopped,
)
from agent_runtime_client.backend.service import ServiceBackend
from agent_runtime_client.capability import CapabilityPrincipal
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.sse import encode_event
from agent_runtime_client.wire import RunEvent, RunRequest

PERSON = CapabilityPrincipal(iss="https://issuer.test", sub="person-1")
PERSON_TOKEN = "person-token-for-the-test"  # noqa: S105 - a stand-in, no secret
WORKLOAD_TOKEN = "workload-token-for-the-test"  # noqa: S105 - a stand-in, no secret


def a_request(**overrides: object) -> RunRequest:
    return RunRequest.model_validate(run_body(**overrides))


def an_asker(*, person: bool = True, tokens_asked: list[None] | None = None) -> Asker:
    async def tokens() -> RunTokens:
        if tokens_asked is not None:
            tokens_asked.append(None)
        return RunTokens(
            person=SecretStr(PERSON_TOKEN) if person else None,
            workload=SecretStr(WORKLOAD_TOKEN),
        )

    return Asker(principal=PERSON if person else None, tokens=tokens)


def a_backend(base_url: str, lifecycle: object | None = None) -> AgentBackend:
    config = AgentConfig.model_validate(
        {"backend": "service", "service_url": base_url, "workload_scope": "agent-service"}
    )
    return ServiceBackend(config, lifecycle)  # type: ignore[arg-type]


class RecordingLifecycle:
    """A lifecycle port that records its calls and refuses runs another turn holds."""

    def __init__(self, held_elsewhere: Iterable[UUID] = ()) -> None:
        self.held_elsewhere = set(held_elsewhere)
        self.registered: list[tuple[UUID, CapabilityPrincipal | None]] = []
        self.released: list[UUID] = []

    def register(self, run_id: UUID, principal: CapabilityPrincipal | None) -> None:
        if run_id in self.held_elsewhere:
            raise RunRegistrationRefused
        self.registered.append((run_id, principal))

    def release(self, run_id: UUID) -> None:
        self.released.append(run_id)


async def turn_outcome(
    backend: AgentBackend, request: RunRequest, asker: Asker
) -> tuple[list[TurnEvent], TurnStopped | None]:
    """Run one turn to its end: the events it yielded and the way it stopped, if it did."""
    events: list[TurnEvent] = []
    try:
        async with aclosing(backend.run(request, asker)) as run:
            async for event in run:
                events.append(event)
    except TurnStopped as stopped:
        return events, stopped
    return events, None


def frames(*events: RunEvent) -> list[bytes]:
    return [encode_event(event) for event in events]


@dataclass
class FakeService:
    """An ASGI app that answers ``POST /runs`` with a scripted status, headers and chunks.

    ``drops`` ends the connection after the chunks without finishing the response, a fault the
    real service never causes. ``silent`` never answers and sets ``disconnected`` when the
    client leaves.
    """

    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: Sequence[bytes] = ()
    drops: bool = False
    silent: bool = False
    disconnected: threading.Event = field(default_factory=threading.Event)
    seen: list[dict[str, str]] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return
        self.seen.append({k.decode(): v.decode() for k, v in scope["headers"]})
        self.bodies.append(await _whole_body(receive))
        if self.silent:
            while (await receive())["type"] != "http.disconnect":
                pass
            self.disconnected.set()
            return
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(k.lower().encode(), v.encode()) for k, v in self.headers.items()],
            }
        )
        for chunk in self.chunks:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        if not self.drops:
            await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _whole_body(receive: Callable[[], Awaitable[dict[str, Any]]]) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            return body
