"""The answer of the ``wait`` tool every app serves: where a member stands now, in full.

An answer is never a list of changes but every place the member is in at the moment it was
read, so a late, repeated or lost answer leaves no stale state behind: the next one is the
truth again. A place the member was removed from is absent; a paused one is listed.
This module is base, so an agent's runner parses answers without the ``mcp-server`` extra.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field

from agent_runtime_client.member import (
    LEASE_SECONDS,
    Invitation,
    PlaceRef,
    Standing,
    StandingState,
)
from agent_runtime_client.turn import StrictModel

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
MAX_CURSOR_LENGTH: Final = 128


class WaitAnswer(StrictModel):
    """Every current standing of the recipient and the cursor taken before they were read."""

    cursor: Annotated[str, Field(max_length=MAX_CURSOR_LENGTH)]
    standings: tuple[Standing, ...]
