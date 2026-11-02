"""What the gateway backend's turn tests share: a controlled clock, a person and a config."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_runtime_client.backend import TurnPerson, TurnStopped
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.turn import Principal

API_GROUP = "/apps/example"
GATEWAY_ORIGIN = "http://gateway.test"


class SkewedLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock the app side can move on, so a deadline needs no waiting."""

    skew_seconds: float = 0

    def time(self) -> float:
        return super().time() + self.skew_seconds


def ending(stopped: TurnStopped | None):
    return None if stopped is None else (stopped.kind, stopped.code)


def a_person(groups: frozenset[str] = frozenset({API_GROUP}), role: str = "user") -> TurnPerson:
    async def member_of() -> frozenset[str]:
        return groups

    principal = Principal(iss="https://issuer.example", sub="s-1")
    return TurnPerson(principal=principal, role=role, groups=member_of)


def a_gateway_config(directory: Path) -> AgentConfig:
    key_file = directory / "key"
    key_file.write_text("stand-in-key-0001\n")
    return AgentConfig.model_validate(
        {
            "backend": "litellm",
            "api_group": API_GROUP,
            "gateway": {"origin": GATEWAY_ORIGIN, "key_file": str(key_file)},
        }
    )
