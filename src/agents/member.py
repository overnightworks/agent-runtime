"""What an app and an agent say to each other about a member, written once.

An app writes these records and an agent's runner reads them: where the member stands in every
place (``Standing``), the window of lines one place shows (``PlaceWindow``), the state of one
place (``PlaceView``), the arguments of accepting and declining an invitation and of speaking and
stopping in a place, what accepting and declining answer when they refuse, what speaking and
stopping answer (a sentence voiced, or written as a line when the voice failed), and what the
window says when people talk over the member (``TalkedOver``) and whether the member's voice at the
place has failed (``VoiceState``). The URIs name the resources that serve them; a URI is no
credential, so a guessed one yields nothing.
This module is base, so a runner parses all of it without the ``mcp-server`` extra.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self
from urllib.parse import quote, unquote, urlsplit

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    AwareDatetime,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from agents.turn import JsonObject, NonEmptyUnicodeText, StrictModel, UnicodeText

__all__ = [
    "ACCEPT_INVITATION_TOOL",
    "DECLINE_INVITATION_TOOL",
    "INVITATION_SECONDS",
    "LEASE_SECONDS",
    "LISTEN_ALLOWANCE_SECONDS",
    "LISTEN_BOUND_SECONDS",
    "SPEAK_TOOL",
    "STANDING_URI",
    "STOP_SPEAKING_TOOL",
    "AcceptInvitationArguments",
    "AcceptInvitationRefusal",
    "AcceptInvitationRefused",
    "DeclineInvitationArguments",
    "DeclineInvitationReason",
    "DeclineInvitationRefusal",
    "DeclineInvitationRefused",
    "FigureRef",
    "Invitation",
    "MemberStanding",
    "ParentPlace",
    "PartialSpeech",
    "PlaceEntry",
    "PlaceRef",
    "PlaceView",
    "PlaceWindow",
    "SpeakAnswer",
    "SpeakArguments",
    "SpeakRefusal",
    "SpeakRefused",
    "Spoken",
    "SpokenLanguage",
    "Standing",
    "StandingState",
    "StopSpeakingArguments",
    "StopSpeakingRefusal",
    "StopSpeakingRefused",
    "StoppedSpeaking",
    "TalkedOver",
    "VoiceName",
    "VoiceState",
    "VoiceStyle",
    "Written",
    "place_ref_from_view_uri",
    "place_ref_from_window_uri",
    "place_view_uri",
    "place_window_uri",
]

LEASE_SECONDS: Final = 120
"""How long a member's lease lasts; each admitted call renews it."""
INVITATION_SECONDS: Final = 120
"""How long an invitation stands before it expires; the app opens it with this length."""
LISTEN_BOUND_SECONDS: Final = 50
"""The longest a listen lasts before it ends with the completion result."""
LISTEN_ALLOWANCE_SECONDS: Final = 5
"""How long an ended listen still counts as one: a runner listens again at once, which takes the
connection and one call of the port (a few milliseconds plus the port's latency), so the answer
never turns no between two listens; far below ``LEASE_SECONDS``."""
ACCEPT_INVITATION_TOOL: Final = "accept_invitation"
"""The name of the tool that accepts an open invitation."""
DECLINE_INVITATION_TOOL: Final = "decline_invitation"
"""The name of the tool that declines an open invitation."""
SPEAK_TOOL: Final = "speak"
"""The name of the tool that says one sentence aloud in a place."""
STOP_SPEAKING_TOOL: Final = "stop_speaking"
"""The name of the tool that cuts the member's current sentence and its queue."""
STANDING_URI: Final = "member://standing"
"""The resource that answers with the member's ``MemberStanding``."""
_PLACE_WINDOW_URI_PREFIX: Final = f"{STANDING_URI}/places/"
_VIEW_SEGMENT: Final = "/view"


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


class ParentPlace(StrictModel):
    """The place a standing's place lies inside: ``app`` is the parent app's MCP resource URL and
    ``place`` the place there. A runner compares both with a place it holds, ``app`` as a parsed
    URL, so equal addresses in another spelling match; the record grants nothing."""

    app: AnyHttpUrl
    place: PlaceRef


def _is_false(value: bool) -> bool:
    return not value


def _is_none(value: object) -> bool:
    return value is None


class Standing(StrictModel):
    """One place, the member's state there and, exactly when invited, the invitation."""

    place: PlaceRef
    state: Annotated[StandingState, Field(strict=False)]
    invitation: Invitation | None = None
    serves_view: bool = Field(default=False, exclude_if=_is_false)
    """Whether the app serves this place's ``PlaceView`` beside its window; both URIs are then
    listed among the member's current resources."""
    parent: ParentPlace | None = Field(default=None, exclude_if=_is_none)
    """The place this place lies inside, if another app's place holds it; the app sets it from
    the invitation on and keeps it while the member holds the place."""

    @model_validator(mode="after")
    def _invitation_exactly_when_invited(self) -> Self:
        if (self.state is StandingState.INVITED) != (self.invitation is not None):
            raise ValueError("an invitation is carried exactly when the state is invited")
        return self


class MemberStanding(StrictModel):
    """Every place the member is in at the moment it was read; a place left is absent."""

    standings: tuple[Standing, ...]


_FORBIDDEN_HOST_CHARACTERS: Final = frozenset('%"<>^`{|}')


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
        message = "a figure url is an absolute https address without user part, query or fragment"
        if (
            not url.isprintable()
            or any(character.isspace() or character == "\\" for character in url)
            or "?" in url
            or "#" in url
        ):
            raise ValueError(message)
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - reading the port is what validates it
        except ValueError as error:
            raise ValueError(message) from error
        if (
            parts.scheme != "https"
            or not parts.hostname
            or "@" in parts.netloc
            or any(character in _FORBIDDEN_HOST_CHARACTERS for character in parts.hostname)
        ):
            raise ValueError(message)
        return url


VoiceName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
"""The name of the voice an agent speaks with: 1 to 64 ASCII letters of either case, digits, ``.``,
``_`` or ``-``. The name is passed through unchanged; the app's speech service decides whether it
offers that voice."""


class AcceptInvitationArguments(StrictModel):
    """The arguments of ``accept_invitation``: the place and the invitation's id and epoch, and
    optionally the agent's ``figure`` and the ``voice`` it speaks with; an absent one means the
    app's own default."""

    place: PlaceRef
    invitation: NonEmptyUnicodeText
    epoch: int = Field(ge=0)
    figure: FigureRef | None = Field(default=None, exclude_if=_is_none)
    voice: VoiceName | None = Field(default=None, exclude_if=_is_none)


class AcceptInvitationRefusal(StrEnum):
    """Why the app does not accept an invitation: ``stale_invitation`` when the invitation is no
    longer standing, has expired, or its id or epoch no longer matches, or the member holds no
    lease; ``two_seats`` when the person who brought the member does not hold exactly
    one seat in the place, the inviting one; ``sponsor_away`` when that person's seat lost its
    connection and waits to resume; ``busy`` when the place has no free seat."""

    STALE_INVITATION = "stale_invitation"
    TWO_SEATS = "two_seats"
    SPONSOR_AWAY = "sponsor_away"
    BUSY = "busy"


class AcceptInvitationRefused(StrictModel):
    """What ``accept_invitation`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[AcceptInvitationRefusal, Field(strict=False)]


class DeclineInvitationReason(StrEnum):
    """Why the runner declines an invitation: ``busy`` when the runner is at its own limit of how
    many places it is in at once; not ``AcceptInvitationRefusal.BUSY``, which is the app's full
    place."""

    BUSY = "busy"


class DeclineInvitationArguments(StrictModel):
    """The arguments of ``decline_invitation``: the place, the invitation's id and epoch, and the
    reason; a decline needs no lease, as it seats nothing."""

    place: PlaceRef
    invitation: NonEmptyUnicodeText
    epoch: int = Field(ge=0)
    reason: Annotated[DeclineInvitationReason, Field(strict=False)]


class DeclineInvitationRefusal(StrEnum):
    """Why the app does not take a decline: ``stale_invitation`` when the invitation is not that
    member's, no longer stands, has expired, or its id or epoch no longer matches."""

    STALE_INVITATION = "stale_invitation"


class DeclineInvitationRefused(StrictModel):
    """What ``decline_invitation`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[DeclineInvitationRefusal, Field(strict=False)]


VoiceStyle = NonEmptyUnicodeText
"""How a sentence should sound: free text, such as ``louder and angrier``. The text is passed
through unchanged; the app neither bounds nor checks it, and its speech service owns the bound
and the check."""

SpokenLanguage = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]
"""The language of a spoken sentence: a lowercase ISO 639-1 code of two letters, such as ``de`` or
``en``. The record checks the form only; whether the app's voice speaks the language is the app's
decision."""


class SpeakArguments(StrictModel):
    """The arguments of ``speak``: the place and one sentence, whose length the app bounds, and
    optionally the ``language`` of that sentence and the ``style`` it should sound in, which the app
    passes on unchanged; an absent one means the app's own default."""

    place: PlaceRef
    text: NonEmptyUnicodeText
    language: SpokenLanguage | None = Field(default=None, exclude_if=_is_none)
    style: VoiceStyle | None = Field(default=None, exclude_if=_is_none)


def _refuse_false(value: bool) -> bool:
    if not value:
        raise ValueError("must be true")
    return value


MarkerTrue = Annotated[StrictBool, AfterValidator(_refuse_false)]
"""A marker field of an answer: exactly the JSON ``true``, so two answers stay distinct as JSON."""


class Spoken(StrictModel):
    """What ``speak`` answers, as JSON and without ``is_error``, when the sentence starts to play;
    the field is exactly the JSON ``true``. It carries no length: the sentence's end reaches the
    member as its own line in the window."""

    spoken: MarkerTrue


class Written(StrictModel):
    """What ``speak`` answers, as JSON and without ``is_error``, when the app could not voice the
    sentence for a failure of its voice and posted it at the place as the member's written line
    instead; the field is exactly the JSON ``true``."""

    written: MarkerTrue


SpeakAnswer = Spoken | Written
"""What a ``speak`` that is not refused answers: the sentence started to play, or it was written."""


class SpeakRefusal(StrEnum):
    """Why the app does not speak a sentence: ``language_unavailable`` when the app's voice does
    not speak the language the sentence names, ``style_unavailable`` for every refusal of the
    ``style`` by the voice (it takes none, it is too long or it is not supported), ``stopped``
    for a queued sentence that a ``stop_speaking`` dropped, and ``unavailable`` when the app
    neither voices nor writes the sentence now: a social reason, a limit of its own or a failed
    voice at a place without a chat. No refusal is ever turned into a written line."""

    NOT_ACTIVE = "not_active"
    TOO_LONG = "too_long"
    LANGUAGE_UNAVAILABLE = "language_unavailable"
    STYLE_UNAVAILABLE = "style_unavailable"
    STOPPED = "stopped"
    UNAVAILABLE = "unavailable"


class SpeakRefused(StrictModel):
    """What ``speak`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[SpeakRefusal, Field(strict=False)]


class StopSpeakingArguments(StrictModel):
    """The arguments of ``stop_speaking``: the place whose sentence and queue of the member are
    cut."""

    place: PlaceRef


class StoppedSpeaking(StrictModel):
    """What ``stop_speaking`` answers on success, as JSON: the text of the cut sentence's line,
    the heard part when the app knows it, else the whole sentence."""

    cut: NonEmptyUnicodeText


class StopSpeakingRefusal(StrEnum):
    """Why the app does not stop: ``not_active`` when the member left, was removed or is paused;
    ``nothing_playing`` when no sentence of the member plays, so nothing was cut or dropped."""

    NOT_ACTIVE = "not_active"
    NOTHING_PLAYING = "nothing_playing"


class StopSpeakingRefused(StrictModel):
    """What ``stop_speaking`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[StopSpeakingRefusal, Field(strict=False)]


class PartialSpeech(StrictModel):
    """What one person has said so far in an utterance that is not finished; provisional, never a
    line. ``text`` is empty while the person has started and said no words yet."""

    name: NonEmptyUnicodeText
    text: UnicodeText


class TalkedOver(StrictModel):
    """People talk over the member. ``id`` is the app's, never repeats within the window's
    ``generation`` and stays fixed from the record's appearance to its removal, so a runner takes
    one decision per ``id``; a second person who starts speaking adds a ``PartialSpeech`` under
    the same ``id``. ``playing`` says some sentence of the member plays at the place now. ``speech``
    lists the people in the order they started speaking."""

    id: NonEmptyUnicodeText
    playing: bool
    speech: tuple[PartialSpeech, ...] = Field(min_length=1)


class VoiceState(StrEnum):
    """Whether the member's voice at the place has failed for that member there: ``available``
    while the app's voice works, ``unavailable`` while it has failed, so a ``speak`` would not be
    voiced for that reason. A social reason or a limit of the app never changes it."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class PlaceEntry(StrictModel):
    """One line of a place; ``kind`` is the app's own text, ``own`` marks the member's line and
    ``spoken`` a line said aloud and heard, one finished utterance, rather than written. A line
    enters once its sentence has ended and is never changed; ``cut`` marks a spoken sentence cut
    before its end, whose ``text`` is the part heard up to the cut, or the whole sentence when the
    app cannot tell how far it played. ``act`` marks a written line that says what someone did
    rather than what someone said: it is told and never calls the member to act."""

    seq: int = Field(ge=1)
    kind: NonEmptyUnicodeText
    name: NonEmptyUnicodeText
    text: NonEmptyUnicodeText
    own: bool
    spoken: bool = False
    cut: bool = Field(default=False, exclude_if=_is_false)
    act: bool = Field(default=False, exclude_if=_is_false)

    @model_validator(mode="after")
    def _only_a_spoken_line_is_cut(self) -> Self:
        if self.cut and not self.spoken:
            raise ValueError("only a spoken line is cut")
        return self

    @model_validator(mode="after")
    def _a_spoken_line_is_no_act(self) -> Self:
        if self.act and self.spoken:
            raise ValueError("a spoken line is no act")
        return self


class PlaceWindow(StrictModel):
    """The lines of one place from ``starts_at`` on, in ascending ``seq``; ``seq`` may skip.

    ``generation`` names the run of ``seq`` numbers the window belongs to. An empty window is
    valid, and its ``starts_at`` is the ``seq`` the next line will get. ``talked_over`` is set
    only while people talk over the member; a person's partial and finished line are never both
    in one read. ``voice_state`` states a failure of the member's voice at the place, for that
    member there; absent, the app states nothing.
    """

    generation: NonEmptyUnicodeText
    starts_at: int = Field(ge=1)
    entries: tuple[PlaceEntry, ...]
    talked_over: TalkedOver | None = Field(default=None, exclude_if=_is_none)
    voice_state: Annotated[VoiceState, Field(strict=False)] | None = Field(
        default=None, exclude_if=_is_none
    )

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


def place_ref_from_window_uri(uri: object) -> PlaceRef | None:
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


class PlaceView(StrictModel):
    """The state of one place for the member, as the app serves it.

    ``state`` is the app's document, carried untouched and never read here. ``revision`` changes
    whenever the place moves on for this member, that is whenever what the member may do or must
    act on changes; parts of ``state`` that say only who is present may change without it.
    ``awaits_you`` says the place waits for this member to act; the app decides it from the state
    at that revision, so one revision has one ``awaits_you``.
    """

    revision: int = Field(ge=0)
    awaits_you: bool
    state: JsonObject


def place_view_uri(place: PlaceRef) -> str:
    """The resource of one place's view: the place's window URI and the segment ``view``."""
    return place_window_uri(place) + _VIEW_SEGMENT


def place_ref_from_view_uri(uri: object) -> PlaceRef | None:
    """The place a view URI names, or ``None`` for anything but its canonical spelling."""
    if not isinstance(uri, str) or not uri.endswith(_VIEW_SEGMENT):
        return None
    return place_ref_from_window_uri(uri.removesuffix(_VIEW_SEGMENT))
