"""The ``wait`` tool an app serves on its person front, and the two ports it reads.

A wait answers at once when its ``after`` is not the recipient's current cursor, else when a
change is signalled for the recipient or its timeout runs out. It takes the cursor first and
reads the recipient's whole standing after, and the app signals a change only once the new
state is visible to that read, so a change after the read always lies after the cursor and
the next wait answers at once. Nothing is consumed, so a cancelled wait loses nothing.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Final, Protocol

import anyio
from pydantic import Field, ValidationError, field_validator

from agent_runtime_client.mcp_server.person import PersonRequest, PersonTool
from agent_runtime_client.tools import ToolDeclaration, ToolEffect, ToolOutcome
from agent_runtime_client.turn import JsonObject, Principal, StrictModel
from agent_runtime_client.wait import (
    MAX_CURSOR_LENGTH,
    STANDINGS_READ_SECONDS,
    WAIT_BOUND_SECONDS,
    Standing,
    WaitAnswer,
)

type CurrentStandings = Callable[[Principal], Awaitable[Sequence[Standing]]]
"""The app's own read of every place a recipient is in now, well inside the read deadline."""


class MemberChanges(Protocol):
    """The signal that something changed for a recipient; it keeps no history."""

    def changed(self, recipient: Principal) -> None:
        """Signals a change, without blocking, once the new state is visible to the read.

        It runs on the event loop that serves the front, or from a thread through
        ``anyio.from_thread.run_sync``.
        """
        ...

    async def wait(self, recipient: Principal, after: str | None, seconds: float) -> str:
        """The recipient's cursor: at once when ``after`` is not it, else after a change or
        ``seconds`` seconds."""
        ...


class InMemoryMemberChanges:
    """``MemberChanges`` for one process: one change counter per recipient that had a change.

    Every cursor carries a token of this run, so a cursor from before a restart answers at once.
    """

    def __init__(self) -> None:
        self._run = secrets.token_hex(8)
        self._changes: dict[Principal, int] = {}
        self._signals: dict[Principal, anyio.Event] = {}

    def changed(self, recipient: Principal) -> None:
        self._changes[recipient] = self._changes.get(recipient, 0) + 1
        signal = self._signals.pop(recipient, None)
        if signal is not None:
            signal.set()

    async def wait(self, recipient: Principal, after: str | None, seconds: float) -> str:
        cursor = self._cursor_of(recipient)
        if after != cursor:
            return cursor
        # No await may stand between the counter check above and this registration, or a
        # change signalled in between would be lost until the timeout.
        signal = self._signals.get(recipient)
        if signal is None:
            signal = self._signals[recipient] = anyio.Event()
        try:
            with anyio.move_on_after(seconds):
                await signal.wait()
        finally:
            self._forget_unawaited(recipient, signal)
        return self._cursor_of(recipient)

    def _cursor_of(self, recipient: Principal) -> str:
        return f"{self._run}-{self._changes.get(recipient, 0)}"

    def _forget_unawaited(self, recipient: Principal, signal: anyio.Event) -> None:
        if self._signals.get(recipient) is signal and signal.statistics().tasks_waiting == 0:
            del self._signals[recipient]


class _WaitArguments(StrictModel):
    after: Annotated[str, Field(max_length=MAX_CURSOR_LENGTH)] | None = None
    timeout_seconds: int = Field(default=WAIT_BOUND_SECONDS, ge=1, le=WAIT_BOUND_SECONDS)

    @field_validator("after", mode="before")
    @classmethod
    def _refuse_null(cls, after: object) -> object:
        if after is None:
            raise ValueError("must be a string when given")
        return after


_DECLARATION: Final = ToolDeclaration(
    name="wait",
    description=(
        "Wait until something changes for you in this app, then get every place you are in now:"
        " invited (with the invitation), active or paused; a place you left is absent. Pass the"
        " cursor of the last answer as after; without it the answer comes at once."
    ),
    parameters={
        "type": "object",
        "properties": {
            "after": {"type": "string", "maxLength": MAX_CURSOR_LENGTH},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": WAIT_BOUND_SECONDS,
                "default": WAIT_BOUND_SECONDS,
            },
        },
        "additionalProperties": False,
    },
    effect=ToolEffect.READ_ONLY,
)


def _refused_names(refusal: ValidationError) -> str:
    return ", ".join(sorted({str(error["loc"][0]) for error in refusal.errors()}))


def wait_tool(changes: MemberChanges, standings: CurrentStandings) -> PersonTool:
    """The ``wait`` tool for the request's own principal.

    A read that fails or runs past ``STANDINGS_READ_SECONDS`` raises, which the person front
    answers as the tool's failed result, never as an empty standing.
    """

    async def answer(request: PersonRequest, arguments: JsonObject) -> ToolOutcome:
        try:
            wanted = _WaitArguments.model_validate(arguments)
        except ValidationError as refusal:
            return ToolOutcome(f"refused arguments: {_refused_names(refusal)}", is_error=True)
        cursor = await changes.wait(request.principal, wanted.after, wanted.timeout_seconds)
        with anyio.fail_after(STANDINGS_READ_SECONDS):
            current = await standings(request.principal)
        waited = WaitAnswer(cursor=cursor, standings=tuple(current))
        return ToolOutcome(waited.model_dump_json(), is_error=False)

    return PersonTool(_DECLARATION, answer)
