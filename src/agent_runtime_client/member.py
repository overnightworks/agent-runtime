"""What an app and an agent say to each other about a member, written once.

An app writes these records and an agent's runner reads them: where the member stands in every
place (``Standing``), the window of lines one place shows (``PlaceWindow``) and the arguments
of accepting an invitation. The URIs name the resources that serve them; a URI is no credential,
so a guessed one yields nothing. This module is base, so a runner parses all of it without the
``mcp-server`` extra.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self
from urllib.parse import quote, unquote

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from agent_runtime_client.turn import NonEmptyUnicodeText, StrictModel

__all__ = [
    "ACCEPT_INVITATION_TOOL",
    "LEASE_SECONDS",
    "LISTEN_BOUND_SECONDS",
    "STANDING_URI",
    "AcceptInvitationArguments",
    "Invitation",
    "MemberStanding",
    "PlaceEntry",
    "PlaceRef",
    "PlaceWindow",
    "Standing",
    "StandingState",
    "place_ref_from_window_uri",
    "place_window_uri",
]

LEASE_SECONDS: Final = 120
"""How long a member's lease lasts after its last call; a loop of waits renews itself."""
LISTEN_BOUND_SECONDS: Final = 50
"""The longest a listen lasts before it ends with the completion result."""
ACCEPT_INVITATION_TOOL: Final = "accept_invitation"
"""The name of the tool that accepts an open invitation."""
STANDING_URI: Final = "member://standing"
"""The resource that answers with the member's ``MemberStanding``."""
_PLACE_WINDOW_URI_PREFIX: Final = f"{STANDING_URI}/places/"


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


class MemberStanding(StrictModel):
    """Every place the member is in at the moment it was read; a place left is absent."""

    standings: tuple[Standing, ...]


class AcceptInvitationArguments(StrictModel):
    """The arguments of ``accept_invitation``: the place and the invitation's id and epoch."""

    place: PlaceRef
    invitation: NonEmptyUnicodeText
    epoch: int = Field(ge=0)


class PlaceEntry(StrictModel):
    """One line of a place; ``kind`` is the app's own text and ``own`` marks the member's line."""

    seq: int = Field(ge=1)
    kind: NonEmptyUnicodeText
    name: NonEmptyUnicodeText
    text: NonEmptyUnicodeText
    own: bool


class PlaceWindow(StrictModel):
    """The lines of one place from ``starts_at`` on, in ascending ``seq``; ``seq`` may skip.

    ``generation`` names the run of ``seq`` numbers the window belongs to. An empty window is
    valid, and its ``starts_at`` is the ``seq`` the next line will get.
    """

    generation: NonEmptyUnicodeText
    starts_at: int = Field(ge=1)
    entries: tuple[PlaceEntry, ...]

    @model_validator(mode="after")
    def _entries_ascend_from_the_start(self) -> Self:
        seqs = [line.seq for line in self.entries]
        if any(later <= earlier for earlier, later in zip(seqs, seqs[1:], strict=False)):
            raise ValueError("entries must have strictly ascending seq")
        if seqs and seqs[0] != self.starts_at:
            raise ValueError("a non-empty window starts at its first entry's seq")
        return self


def place_window_uri(place: PlaceRef) -> str:
    """The resource of one place's window; each segment is escaped, so ``/`` stays opaque."""
    return _PLACE_WINDOW_URI_PREFIX + f"{quote(place.kind, safe='')}/{quote(place.id, safe='')}"


def place_ref_from_window_uri(uri: str) -> PlaceRef | None:
    """The place a window URI names, or ``None`` for anything but its canonical spelling."""
    if not isinstance(uri, str) or not uri.startswith(_PLACE_WINDOW_URI_PREFIX):
        return None
    segments = uri.removeprefix(_PLACE_WINDOW_URI_PREFIX).split("/")
    if len(segments) != 2 or not all(segments):
        return None
    try:
        kind, place_id = (unquote(segment, errors="strict") for segment in segments)
        place = PlaceRef(kind=kind, id=place_id)
    except UnicodeDecodeError, ValidationError:
        return None
    return place if place_window_uri(place) == uri else None
