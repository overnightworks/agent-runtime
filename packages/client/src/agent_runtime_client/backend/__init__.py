"""How an app runs one agent turn: the port, who asks, and how a turn ends short.

``AgentBackend`` is the port an app consumes inside ``contextlib.aclosing``:
closing the generator runs the backend's cleanup. This module imports no HTTP library;
``backend.gateway`` and ``backend.fake_gateway`` need the ``backend`` extra.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from agent_runtime_client.agents import TurnRequest
from agent_runtime_client.turn import AgentRole, FailureCode, Principal, RejectionCode
from agent_runtime_client.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)

TurnEvent = AssistantTextEvent | ToolCallEvent | ToolResultEvent | FinalEvent

MAX_RETRY_AFTER_S: Final = 600


GroupSource = Callable[[], Awaitable[frozenset[str]]]


@dataclass(frozen=True)
class TurnPerson:
    """The verified person a turn is for: who they are, their app role and their groups."""

    principal: Principal
    role: AgentRole
    groups: GroupSource


class TurnStopKind(StrEnum):
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnStopped(Exception):
    """The one way a turn ends short of its answer.

    ``code`` is a ``RejectionCode`` when refused, a ``FailureCode`` when failed and none when
    cancelled. ``detail`` is a closed label, never text from the wire or a token.
    """

    def __init__(
        self,
        kind: TurnStopKind,
        code: RejectionCode | FailureCode | None = None,
        *,
        retry_after_s: int | None = None,
        after_side_effect: bool = False,
        detail: str | None = None,
    ) -> None:
        super().__init__(f"{kind.value}: {code.value if code else 'no code'}")
        self.kind = kind
        self.code = code
        self.retry_after_s = retry_after_s
        self.after_side_effect = after_side_effect
        self.detail = detail


class AgentBackend(Protocol):
    def run(self, request: TurnRequest, person: TurnPerson) -> AsyncGenerator[TurnEvent, None]:
        """Stream the turn's events; raise ``TurnStopped`` when it ends short of its answer."""
