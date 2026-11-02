"""How an app runs one agent turn: the port, who asks, and how a turn ends short.

``AgentBackend`` is the port an app consumes inside ``contextlib.aclosing``:
closing the generator runs the backend's cleanup. ``ServiceBackend``
(``agent_runtime_client.backend.service``, the ``backend`` extra) is the one
implementation, so this package imports no HTTP library.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from pydantic import SecretStr

from agent_runtime_client.capability import CapabilityPrincipal
from agent_runtime_client.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_runtime_client.wire import FailureCode, RejectionCode, RunRequest

TurnEvent = AssistantTextEvent | ToolCallEvent | ToolResultEvent | FinalEvent

MAX_RETRY_AFTER_S: Final = 600


@dataclass(frozen=True)
class RunTokens:
    """The tokens one run sends: the person's, or none for an app's own run, and the app's."""

    person: SecretStr | None
    workload: SecretStr


RunTokenSource = Callable[[], Awaitable[RunTokens]]


@dataclass(frozen=True)
class Asker:
    """Who a run is for: the verified person (none for an app's own run) and their tokens."""

    principal: CapabilityPrincipal | None
    tokens: RunTokenSource


class RunRegistrationRefused(Exception):
    """The lifecycle already holds this run for another turn."""


class ServiceRunLifecycle(Protocol):
    """What an app keeps for the length of a run, such as the right to answer its tool calls."""

    def register(self, run_id: UUID, principal: CapabilityPrincipal | None) -> None:
        """Hold the run; raise ``RunRegistrationRefused`` when another turn holds it."""

    def release(self, run_id: UUID) -> None:
        """Let go of a run this turn registered."""


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
    def run(self, request: RunRequest, asker: Asker) -> AsyncGenerator[TurnEvent, None]:
        """Stream the turn's events; raise ``TurnStopped`` when it ends short of its answer."""
