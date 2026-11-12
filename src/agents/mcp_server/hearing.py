"""Which member hears which speaker, and the one line a heard utterance becomes.

An app that lets an agent hear people at a place records its own rule's decisions with
``MemberHearing.allow``; the standard is ``docs/hearing.md``. ``heard`` answers which members
hear a finished utterance and returns the line to append; the app adds ``seq``, appends the line
to those members' windows and signals the change. The record lives in memory, in one process, and
is called on the front's event loop or through ``anyio.from_thread.run_sync``, as
``ResourceChanges`` is.

A member hears an utterance only if the member's entry exists when ``heard`` runs and was switched
on no later than the utterance began. An end deletes entries, so no utterance finishing after it
reaches anyone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from agents.member import PlaceRef
from agents.turn import NonEmptyUnicodeText, Principal, StrictModel


class Sense(StrEnum):
    """What a member may sense of a speaker; only hearing is defined."""

    HEARING = "hearing"


class Utterance(StrictModel):
    """A finished utterance. ``speaker`` is the app's own opaque key; ``began_at`` is a lower
    bound of the sound's start on the clock of ``MemberHearing``."""

    place: PlaceRef
    speaker: NonEmptyUnicodeText
    kind: NonEmptyUnicodeText
    name: NonEmptyUnicodeText
    text: NonEmptyUnicodeText
    began_at: Annotated[float, Field(allow_inf_nan=False)]


class SpokenLine(StrictModel):
    """A line said aloud and heard, as a ``PlaceEntry`` without the ``seq`` the app adds."""

    kind: NonEmptyUnicodeText
    name: NonEmptyUnicodeText
    text: NonEmptyUnicodeText
    own: Literal[False] = False
    spoken: Literal[True] = True


class Heard(StrictModel):
    """The members that hear an utterance and the one line to append to each of their windows."""

    members: frozenset[Principal]
    line: SpokenLine


def _loop_time() -> float:
    return asyncio.get_running_loop().time()


class MemberHearing:
    """The record of who may hear whom. ``now`` is the clock of the app's ``began_at`` values; by
    default the running loop's time, read when ``allow`` runs, so the record can be built before
    the loop runs."""

    def __init__(self, now: Callable[[], float] | None = None) -> None:
        self._now = _loop_time if now is None else now
        self._switched_on_at: dict[PlaceRef, dict[tuple[str, Sense], dict[Principal, float]]] = {}

    def allow(
        self, place: PlaceRef, speaker: str, member: Principal, sense: Sense, allowed: bool
    ) -> None:
        """Record the app's decision. Switching on while on keeps the earlier time; off deletes."""
        if allowed:
            ears = self._switched_on_at.setdefault(place, {})
            ears.setdefault((speaker, sense), {}).setdefault(member, self._now())
        else:
            self._ear(place, speaker, sense).pop(member, None)
            self._prune(place)

    def heard(self, utterance: Utterance) -> Heard:
        """The members that hear the utterance now, with its line."""
        switched_on_at = self._ear(utterance.place, utterance.speaker, Sense.HEARING)
        return Heard(
            members=frozenset(
                member for member, since in switched_on_at.items() if since <= utterance.began_at
            ),
            line=SpokenLine(kind=utterance.kind, name=utterance.name, text=utterance.text),
        )

    def member_ended(self, place: PlaceRef, member: Principal) -> None:
        """The member left the place or was removed from it."""
        for switched_on_at in self._switched_on_at.get(place, {}).values():
            switched_on_at.pop(member, None)
        self._prune(place)

    def speaker_ended(self, place: PlaceRef, speaker: str) -> None:
        """The speaker left the place."""
        ears = self._switched_on_at.get(place, {})
        for ear in [ear for ear in ears if ear[0] == speaker]:
            del ears[ear]
        self._prune(place)

    def place_ended(self, place: PlaceRef) -> None:
        """The place ended."""
        self._switched_on_at.pop(place, None)

    def allows(self, place: PlaceRef, speaker: str, member: Principal, sense: Sense) -> bool:
        """Whether the member's switch is on for the speaker."""
        return member in self._ear(place, speaker, sense)

    def sensing(self, place: PlaceRef, speaker: str, sense: Sense) -> frozenset[Principal]:
        """The members whose switch is on for the speaker."""
        return frozenset(self._ear(place, speaker, sense))

    def _ear(self, place: PlaceRef, speaker: str, sense: Sense) -> dict[Principal, float]:
        return self._switched_on_at.get(place, {}).get((speaker, sense), {})

    def _prune(self, place: PlaceRef) -> None:
        ears = self._switched_on_at.get(place, {})
        for ear in [ear for ear, switched_on_at in ears.items() if not switched_on_at]:
            del ears[ear]
        if not ears:
            self._switched_on_at.pop(place, None)
