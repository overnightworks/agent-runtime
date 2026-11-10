"""What an app and an agent say to each other about a member, written once.

An app writes these records and an agent's runner reads them: where the member stands in every
place (``Standing``), the window of lines one place shows (``PlaceWindow``), the arguments of
accepting an invitation and of speaking in a place, what accepting answers when it refuses, and
what speaking answers. The URIs name the resources that serve them; a URI is no credential, so a
guessed one yields nothing. This module is base, so a runner parses all of it without the
``mcp-server`` extra.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self
from urllib.parse import quote, unquote, urlsplit

from pydantic import (
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from agents.turn import NonEmptyUnicodeText, StrictModel

__all__ = [
    "ACCEPT_INVITATION_TOOL",
    "LEASE_SECONDS",
    "LISTEN_BOUND_SECONDS",
    "SPEAK_TOOL",
    "STANDING_URI",
    "AcceptInvitationArguments",
    "AcceptInvitationRefusal",
    "AcceptInvitationRefused",
    "FigureRef",
    "Invitation",
    "MemberStanding",
    "PlaceEntry",
    "PlaceRef",
    "PlaceWindow",
    "SpeakArguments",
    "SpeakRefusal",
    "SpeakRefused",
    "Spoken",
    "Standing",
    "StandingState",
    "VoiceName",
    "place_ref_from_window_uri",
    "place_window_uri",
]

LEASE_SECONDS: Final = 120
"""How long a member's lease lasts; each admitted call renews it."""
LISTEN_BOUND_SECONDS: Final = 50
"""The longest a listen lasts before it ends with the completion result."""
ACCEPT_INVITATION_TOOL: Final = "accept_invitation"
"""The name of the tool that accepts an open invitation."""
SPEAK_TOOL: Final = "speak"
"""The name of the tool that says one sentence aloud in a place."""
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


class FigureRef(StrictModel):
    """Where an agent's 3D figure is and the digest its bytes must have: ``url`` is an absolute
    ``https`` address with a host and no user part, query or fragment, and ``sha256`` is 64
    lowercase hexadecimal characters. The record checks the form only; whether to fetch the
    figure is the app's decision."""

    url: str
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("url")
    @classmethod
    def _url_is_a_plain_https_address(cls, url: str) -> str:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or "@" in parts.netloc
            or "?" in url
            or "#" in url
        ):
            message = (
                "a figure url is an absolute https address without user part, query or fragment"
            )
            raise ValueError(message)
        return url


VoiceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9._-]{1,64}$")]
"""The name of the voice an agent speaks with: 1 to 64 lowercase letters, digits, ``.``, ``_`` or
``-``. The app's speech service decides whether it offers that voice."""


class AcceptInvitationArguments(StrictModel):
    """The arguments of ``accept_invitation``: the place and the invitation's id and epoch, and
    optionally the agent's ``figure`` and the ``voice`` it speaks with; an absent one means the
    app's own default."""

    place: PlaceRef
    invitation: NonEmptyUnicodeText
    epoch: int = Field(ge=0)
    figure: FigureRef | None = None
    voice: VoiceName | None = None

    @model_serializer(mode="wrap")
    def _omit_what_is_absent(self, serialize: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Dump an absent ``figure`` or ``voice`` as no key, so an accept without them keeps the
        wire a host without the fields parses."""
        fields = serialize(self)
        return {name: value for name, value in fields.items() if value is not None}


class AcceptInvitationRefusal(StrEnum):
    """Why the app does not accept an invitation: ``stale_invitation`` when the invitation is no
    longer standing, has expired, or its id or epoch no longer matches, or the member holds no
    lease; ``two_seats`` when the person who brought the member does not hold exactly
    one seat in the place, the inviting one; ``sponsor_away`` when that person's seat lost its
    connection and waits to resume; ``busy`` when the place or the app already holds as many members
    as it takes, or that person already brings another."""

    STALE_INVITATION = "stale_invitation"
    TWO_SEATS = "two_seats"
    SPONSOR_AWAY = "sponsor_away"
    BUSY = "busy"


class AcceptInvitationRefused(StrictModel):
    """What ``accept_invitation`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[AcceptInvitationRefusal, Field(strict=False)]


class SpeakArguments(StrictModel):
    """The arguments of ``speak``: the place and one sentence, whose length the app bounds."""

    place: PlaceRef
    text: NonEmptyUnicodeText


class Spoken(StrictModel):
    """What ``speak`` answers on success, as JSON: how long the sentence plays."""

    duration_ms: int = Field(ge=1)


class SpeakRefusal(StrEnum):
    """Why the app does not speak a sentence."""

    NOT_ACTIVE = "not_active"
    TOO_LONG = "too_long"
    UNAVAILABLE = "unavailable"


class SpeakRefused(StrictModel):
    """What ``speak`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[SpeakRefusal, Field(strict=False)]


class PlaceEntry(StrictModel):
    """One line of a place; ``kind`` is the app's own text, ``own`` marks the member's line and
    ``spoken`` a line said aloud and heard, one finished utterance, rather than written."""

    seq: int = Field(ge=1)
    kind: NonEmptyUnicodeText
    name: NonEmptyUnicodeText
    text: NonEmptyUnicodeText
    own: bool
    spoken: bool = False


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
