"""``MemberHearing``: which member hears which speaker, and the one line a heard utterance becomes.

The tests name the rules of ``docs/hearing.md`` by their IDs. A fake clock stands for the one
clock the app's ``began_at`` and the record's switch-on times share.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from agents.mcp_server import Heard, MemberHearing, Sense, SpokenLine, Utterance
from agents.member import PlaceEntry, PlaceRef
from agents.turn import Principal
from tests.support.mcp_server_support import a_member

FAR_LATER = 1000.0

alice = a_member("alice")
bob = a_member("bob")
room = PlaceRef(kind="room", id="first")
other_room = PlaceRef(kind="room", id="second")


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def hearing(clock: Clock) -> MemberHearing:
    return MemberHearing(clock)


def allow_at(
    hearing: MemberHearing,
    clock: Clock,
    seconds: float,
    *,
    member: Principal = alice,
    place: PlaceRef = room,
    speaker: str = "seat-1",
    allowed: bool = True,
) -> None:
    clock.seconds = seconds
    hearing.allow(place, speaker, member, Sense.HEARING, allowed)


def utterance(
    began_at: float, *, place: PlaceRef = room, speaker: str = "seat-1", **fields: Any
) -> Utterance:
    return Utterance(
        place=place,
        speaker=speaker,
        kind=fields.pop("kind", "chat"),
        name=fields.pop("name", "Carol"),
        text=fields.pop("text", "hello there"),
        began_at=began_at,
        **fields,
    )


def who_hears(
    hearing: MemberHearing, began_at: float, *, place: PlaceRef = room, speaker: str = "seat-1"
) -> frozenset[Principal]:
    return hearing.heard(utterance(began_at, place=place, speaker=speaker)).members


def test_an_allowed_member_hears_an_utterance_that_began_after_the_switch_on(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0)

    assert who_hears(hearing, 2.0) == {alice}


def test_nobody_hears_before_an_allow_is_recorded(hearing: MemberHearing) -> None:
    assert who_hears(hearing, FAR_LATER) == frozenset()


@pytest.mark.parametrize(
    ("switched_on_at", "began_at", "heard"),
    [(1.0, 2.0, True), (2.0, 2.0, True), (3.0, 2.0, False)],
    ids=["before the start", "at the start", "after the start"],
)
def test_a_member_hears_an_utterance_only_if_the_switch_on_is_no_later_than_its_start(
    hearing: MemberHearing, clock: Clock, switched_on_at: float, began_at: float, heard: bool
) -> None:
    allow_at(hearing, clock, switched_on_at)

    assert (alice in who_hears(hearing, began_at)) is heard


def test_a_switch_off_before_heard_runs_loses_the_utterance(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0)
    allow_at(hearing, clock, 2.0, allowed=False)

    assert who_hears(hearing, 1.5) == frozenset()


@pytest.mark.parametrize(
    ("began_at", "heard"),
    [(1.5, False), (3.0, True)],
    ids=["began before the second switch-on", "began at the second switch-on"],
)
def test_a_switch_off_and_on_again_hides_only_what_began_before_the_second_switch_on(
    hearing: MemberHearing, clock: Clock, began_at: float, heard: bool
) -> None:
    allow_at(hearing, clock, 1.0)
    allow_at(hearing, clock, 2.0, allowed=False)
    allow_at(hearing, clock, 3.0)

    assert (alice in who_hears(hearing, began_at)) is heard


def test_a_second_allow_while_on_keeps_the_earlier_switch_on_time(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0)
    allow_at(hearing, clock, 5.0)

    assert who_hears(hearing, 2.0) == {alice}


def test_a_switch_off_that_was_never_on_changes_nothing(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0, member=bob, allowed=False)
    allow_at(hearing, clock, 1.0)

    assert who_hears(hearing, 2.0) == {alice}


def test_each_member_hears_by_their_own_switch_on_time(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0, member=alice)
    allow_at(hearing, clock, 3.0, member=bob)

    assert (who_hears(hearing, 2.0), who_hears(hearing, 4.0)) == ({alice}, {alice, bob})


def test_switching_one_member_off_leaves_the_other_hearing(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0, member=alice)
    allow_at(hearing, clock, 1.0, member=bob)
    allow_at(hearing, clock, 2.0, member=alice, allowed=False)

    assert who_hears(hearing, 3.0) == {bob}


def test_a_switch_on_at_one_place_is_not_a_switch_on_at_another(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0, place=room)

    assert (
        who_hears(hearing, 2.0, place=room),
        who_hears(hearing, 2.0, place=other_room),
    ) == ({alice}, frozenset())


def test_one_speaker_key_at_two_places_is_two_speakers(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0, place=room, member=alice)
    allow_at(hearing, clock, 1.0, place=other_room, member=bob)

    assert (
        who_hears(hearing, 2.0, place=room),
        who_hears(hearing, 2.0, place=other_room),
    ) == ({alice}, {bob})


def test_a_switch_on_for_one_speaker_is_not_one_for_another_speaker(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0, speaker="seat-1")

    assert who_hears(hearing, 2.0, speaker="seat-2") == frozenset()


def test_heard_builds_one_spoken_line_of_the_utterance_for_the_hearing_members(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0)

    heard = hearing.heard(utterance(2.0, kind="chat", name="Carol", text="hello there"))

    assert heard == Heard(
        members=frozenset({alice}),
        line=SpokenLine(kind="chat", name="Carol", text="hello there"),
    )


def test_the_line_is_spoken_and_not_the_members_own_and_becomes_a_place_entry_once_seq_is_added(
    hearing: MemberHearing,
) -> None:
    line = hearing.heard(utterance(2.0)).line

    entry = PlaceEntry(seq=1, **line.model_dump())

    assert (entry.spoken, entry.own) == (True, False)


def _member_ended(hearing: MemberHearing, member: Principal, speaker: str) -> None:
    hearing.member_ended(room, member)


def _speaker_ended(hearing: MemberHearing, member: Principal, speaker: str) -> None:
    hearing.speaker_ended(room, speaker)


def _place_ended(hearing: MemberHearing, member: Principal, speaker: str) -> None:
    hearing.place_ended(room)


EndOfHearing = Callable[[MemberHearing, Principal, str], None]
ENDS = pytest.mark.parametrize(
    "end",
    [_member_ended, _speaker_ended, _place_ended],
    ids=["member_ended", "speaker_ended", "place_ended"],
)


@ENDS
def test_an_end_ends_hearing_at_once_for_an_utterance_that_finishes_after_it(
    hearing: MemberHearing, clock: Clock, end: EndOfHearing
) -> None:
    allow_at(hearing, clock, 1.0)

    end(hearing, alice, "seat-1")

    assert who_hears(hearing, 2.0) == frozenset()


@ENDS
@pytest.mark.parametrize(
    ("began_at", "heard"),
    [(4.0, False), (6.0, True)],
    ids=["an utterance from before the rejoin", "an utterance from after the rejoin"],
)
def test_after_a_rejoin_only_an_utterance_that_began_after_the_new_allow_is_heard(
    hearing: MemberHearing, clock: Clock, end: EndOfHearing, began_at: float, heard: bool
) -> None:
    allow_at(hearing, clock, 1.0)
    end(hearing, alice, "seat-1")
    allow_at(hearing, clock, 5.0)

    assert (alice in who_hears(hearing, began_at)) is heard


@pytest.mark.parametrize(
    ("end", "still_hearing"),
    [
        (_member_ended, {"first": {bob}, "second": frozenset(), "elsewhere": {alice}}),
        (_speaker_ended, {"first": frozenset(), "second": {alice}, "elsewhere": {alice}}),
        (_place_ended, {"first": frozenset(), "second": frozenset(), "elsewhere": {alice}}),
    ],
    ids=["member_ended", "speaker_ended", "place_ended"],
)
def test_an_end_reaches_only_its_member_speaker_or_place(
    hearing: MemberHearing,
    clock: Clock,
    end: EndOfHearing,
    still_hearing: dict[str, frozenset[Principal]],
) -> None:
    allow_at(hearing, clock, 1.0, member=alice, speaker="seat-1")
    allow_at(hearing, clock, 1.0, member=bob, speaker="seat-1")
    allow_at(hearing, clock, 1.0, member=alice, speaker="seat-2")
    allow_at(hearing, clock, 1.0, member=alice, place=other_room, speaker="seat-1")

    end(hearing, alice, "seat-1")

    assert {
        "first": who_hears(hearing, 2.0, speaker="seat-1"),
        "second": who_hears(hearing, 2.0, speaker="seat-2"),
        "elsewhere": who_hears(hearing, 2.0, place=other_room, speaker="seat-1"),
    } == still_hearing


@ENDS
def test_an_end_with_nothing_recorded_changes_nothing(
    hearing: MemberHearing, clock: Clock, end: EndOfHearing
) -> None:
    allow_at(hearing, clock, 1.0, member=bob, place=other_room, speaker="seat-2")

    end(hearing, alice, "seat-1")

    assert who_hears(hearing, 2.0, place=other_room, speaker="seat-2") == {bob}


def test_allows_and_sensing_answer_as_heard_does_for_an_utterance_after_every_switch_on(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 1.0, member=alice)
    allow_at(hearing, clock, 2.0, member=bob)
    allow_at(hearing, clock, 3.0, member=bob, allowed=False)

    assert (
        hearing.allows(room, "seat-1", alice, Sense.HEARING),
        hearing.allows(room, "seat-1", bob, Sense.HEARING),
        hearing.sensing(room, "seat-1", Sense.HEARING),
    ) == (True, False, who_hears(hearing, FAR_LATER))


@ENDS
def test_allows_and_sensing_answer_nobody_after_an_end(
    hearing: MemberHearing, clock: Clock, end: EndOfHearing
) -> None:
    allow_at(hearing, clock, 1.0)

    end(hearing, alice, "seat-1")

    assert (
        hearing.allows(room, "seat-1", alice, Sense.HEARING),
        hearing.sensing(room, "seat-1", Sense.HEARING),
    ) == (False, frozenset())


def test_sensing_names_a_member_whose_switch_on_is_later_than_a_running_utterance(
    hearing: MemberHearing, clock: Clock
) -> None:
    allow_at(hearing, clock, 5.0)

    assert (hearing.sensing(room, "seat-1", Sense.HEARING), who_hears(hearing, 4.0)) == (
        {alice},
        frozenset(),
    )


@pytest.mark.parametrize("field", ["speaker", "kind", "name", "text"])
def test_an_utterance_with_an_empty_speaker_key_kind_name_or_text_is_refused(field: str) -> None:
    fields: dict[str, Any] = {
        "place": room,
        "speaker": "seat-1",
        "kind": "chat",
        "name": "Carol",
        "text": "hello there",
        "began_at": 1.0,
        field: "",
    }

    with pytest.raises(ValidationError):
        Utterance(**fields)


@pytest.mark.parametrize("began_at", [math.nan, math.inf, -math.inf])
def test_an_utterance_with_a_start_that_is_not_a_finite_number_is_refused(
    began_at: float,
) -> None:
    with pytest.raises(ValidationError):
        utterance(began_at)


def test_the_default_clock_is_the_running_loops_time_read_when_allow_runs() -> None:
    hearing = MemberHearing()

    async def hear_around_a_switch_on() -> tuple[bool, bool]:
        loop_time = asyncio.get_running_loop().time
        hearing.allow(room, "seat-1", alice, Sense.HEARING, True)
        switched_on_at = loop_time()
        return (
            alice in who_hears(hearing, switched_on_at - 1.0),
            alice in who_hears(hearing, switched_on_at + 1.0),
        )

    assert asyncio.run(hear_around_a_switch_on()) == (False, True)
