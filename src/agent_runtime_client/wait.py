"""The answer of the ``wait`` tool every app serves: where a member stands now, in full.

An answer is never a list of changes but every place the member is in at the moment it was
read, so a late, repeated or lost answer leaves no stale state behind: the next one is the
truth again. A place the member was removed from is absent; a paused one is listed.
This module is base, so an agent's runner parses answers without the ``mcp-server`` extra.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import AwareDatetime, Field, model_validator

from agent_runtime_client.turn import NonEmptyUnicodeText, StrictModel

__all__ = [
    "LEASE_SECONDS",
    "MAX_CURSOR_LENGTH",
    "STANDINGS_READ_SECONDS",
    "WAIT_BOUND_SECONDS",
    "Invitation",
    "PlaceRef",
    "Standing",
    "StandingState",
    "WaitAnswer",
]

WAIT_BOUND_SECONDS: Final = 50
"""The longest a wait lasts before it answers the unchanged standing."""
STANDINGS_READ_SECONDS: Final = 5
"""The longest an app's read of the standings may take before the call fails."""
LEASE_SECONDS: Final = 120
"""How long a member's lease lasts after its last call; a loop of waits renews itself."""
MAX_CURSOR_LENGTH: Final = 128


class PlaceRef(StrictModel):
    """A place the member is in, opaque to everyone but the app: its kind and its id."""

    kind: NonEmptyUnicodeText
    id: NonEmptyUnicodeText


class StandingState(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    PAUSED = "paused"


class Invitation(StrictModel):
    """An open invitation; ``epoch`` is the place's removal epoch, so an accept after a removal
    is refused by the app."""

    id: NonEmptyUnicodeText
    epoch: int = Field(ge=0)
    expires_at: AwareDatetime


class Standing(StrictModel):
    """One place, the member's state there and, exactly when invited, the invitation."""

    place: PlaceRef
    state: Annotated[StandingState, Field(strict=False)]
    invitation: Invitation | None = None

    @model_validator(mode="after")
    def _invitation_exactly_when_invited(self) -> Self:
        if (self.state is StandingState.INVITED) != (self.invitation is not None):
            raise ValueError("an invitation is carried exactly when the state is invited")
        return self


class WaitAnswer(StrictModel):
    """Every current standing of the recipient and the cursor taken before they were read."""

    cursor: Annotated[str, Field(max_length=MAX_CURSOR_LENGTH)]
    standings: tuple[Standing, ...]
