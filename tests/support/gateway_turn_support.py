"""What the gateway backend's turn tests share: a controlled clock, a person and a config."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2

from agent_runtime_client.backend import TurnPerson, TurnStopped
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.turn import Principal

API_GROUP = "/apps/example"
GATEWAY_ORIGIN = "http://gateway.test"
GATEWAY_KEY = "stand-in-key-0001"


class SkewedLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock the app side can move on, so a deadline needs no waiting."""

    skew_seconds: float = 0

    def time(self) -> float:
        return super().time() + self.skew_seconds


class FailingTransport(httpx2.AsyncBaseTransport):
    """Refuses every request with ``error``; it counts the attempts."""

    def __init__(self, error: Exception | None = None) -> None:
        self.attempts = 0
        self._error = error or httpx2.ConnectError("refused")

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
