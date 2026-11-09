"""What an app and an agent say to each other about a member: records, window and URIs."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from agent_runtime_client.member import (
    LEASE_SECONDS,
    LISTEN_BOUND_SECONDS,
    STANDING_URI,
    AcceptInvitationArguments,
    AcceptInvitationRefusal,
    AcceptInvitationRefused,
    Invitation,
    MemberStanding,
    PlaceEntry,
    PlaceRef,
    PlaceWindow,
    SpeakArguments,
    SpeakRefusal,
    SpeakRefused,
    Spoken,
    Standing,
    StandingState,
    place_ref_from_window_uri,
    place_window_uri,
)

PLACE = PlaceRef(kind="room", id="r-1")
INVITATION = Invitation(id="i-1", epoch=3, expires_at=datetime(2026, 11, 8, 12, tzinfo=UTC))
HOSTILE_PLACES = [
    PlaceRef(kind="a/b", id="c/d"),
    PlaceRef(kind="100%", id="%2F"),
    PlaceRef(kind="with space", id=" "),
    PlaceRef(kind="Straße", id="日本語"),
    PlaceRef(kind="?#&=", id="..\\"),
]


def window_from(wire: dict[str, Any]) -> PlaceWindow:
    return PlaceWindow.model_validate_json(json.dumps(wire))


def entry(seq: int, **fields: Any) -> dict[str, Any]:
    return {"seq": seq, "kind": "said", "name": "ada", "text": "hello", "own": False} | fields


def speak_wire(**fields: Any) -> dict[str, Any]:
    return {"place": {"kind": "room", "id": "r-1"}, "text": "Shall we start?"} | fields


@pytest.mark.parametrize(
    ("state", "invitation"),
    [
        (StandingState.INVITED, None),
        (StandingState.ACTIVE, INVITATION),
        (StandingState.PAUSED, INVITATION),
    ],
)
def test_a_standing_carrying_an_invitation_other_than_exactly_when_invited_is_refused(
    state: StandingState, invitation: Invitation | None
) -> None:
    with pytest.raises(ValidationError):
        Standing(place=PLACE, state=state, invitation=invitation)


@pytest.mark.parametrize(
    "fields",
    [
        {"epoch": -1},
        {"epoch": 1.0},
        {"expires_at": datetime(2026, 11, 8, 12)},
        {"id": ""},
    ],
    ids=["negative-epoch", "fractional-epoch", "naive-expiry", "empty-id"],
)
def test_an_invitation_with_a_malformed_field_is_refused(fields: dict[str, Any]) -> None:
    malformed = INVITATION.model_dump() | fields

    with pytest.raises(ValidationError):
        Invitation.model_validate(malformed)


def test_the_lease_and_the_listen_bound_are_positive_seconds() -> None:
    assert LEASE_SECONDS > 0
    assert 0 < LISTEN_BOUND_SECONDS


def test_a_member_standing_reads_back_from_its_json_unchanged() -> None:
    standing = MemberStanding(
        standings=(Standing(place=PLACE, state=StandingState.INVITED, invitation=INVITATION),)
    )

    assert MemberStanding.model_validate_json(standing.model_dump_json()) == standing


def test_a_member_standing_carries_no_cursor() -> None:
    wire_text = json.dumps({"standings": [], "cursor": "c"})

    with pytest.raises(ValidationError):
        MemberStanding.model_validate_json(wire_text)


def test_a_place_window_reads_back_from_its_json_unchanged() -> None:
    window = window_from(
        {"generation": "g-1", "starts_at": 4, "entries": [entry(4), entry(7, own=True)]}
    )

    assert PlaceWindow.model_validate_json(window.model_dump_json()) == window


def test_a_place_window_with_an_unknown_member_is_refused() -> None:
    wire_text = json.dumps({"generation": "g", "starts_at": 1, "entries": [], "extra": 1})

    with pytest.raises(ValidationError):
        PlaceWindow.model_validate_json(wire_text)


def test_an_entry_with_an_unknown_member_is_refused() -> None:
    wire_text = json.dumps(entry(1, extra=1))

    with pytest.raises(ValidationError):
        PlaceEntry.model_validate_json(wire_text)


def test_an_empty_window_is_valid_and_starts_at_the_next_seq() -> None:
    window = PlaceWindow(generation="g", starts_at=9, entries=())

    assert window.starts_at == 9


def test_a_window_with_gaps_in_seq_is_valid_and_a_long_text_is_kept() -> None:
    text = "x" * 100_000

    window = window_from(
        {"generation": "g", "starts_at": 2, "entries": [entry(2, text=text), entry(10)]}
    )

    assert [e.seq for e in window.entries] == [2, 10]
    assert window.entries[0].text == text


@pytest.mark.parametrize(
    ("fields", "spoken"),
    [({"spoken": True}, True), ({"spoken": False}, False), ({}, False)],
    ids=["spoken", "written", "absent"],
)
def test_an_entry_reads_spoken_as_sent_and_an_absent_one_as_written(
    fields: dict[str, Any], spoken: bool
) -> None:
    wire_text = json.dumps(entry(1) | fields)

    assert PlaceEntry.model_validate_json(wire_text).spoken is spoken


def test_a_window_dumped_as_json_carries_spoken_on_every_entry() -> None:
    window = window_from(
        {"generation": "g", "starts_at": 1, "entries": [entry(1), entry(2, spoken=True)]}
    )

    dumped = json.loads(window.model_dump_json())

    assert [line["spoken"] for line in dumped["entries"]] == [False, True]


@pytest.mark.parametrize(
    "fields",
    [
        {"seq": 0},
        {"seq": 1.0},
        {"kind": ""},
        {"name": ""},
        {"text": ""},
        {"own": "yes"},
        {"spoken": "yes"},
    ],
    ids=[
        "zero-seq",
        "fractional-seq",
        "empty-kind",
        "empty-name",
        "empty-text",
        "text-own",
        "text-spoken",
    ],
)
def test_an_entry_with_a_malformed_field_is_refused(fields: dict[str, Any]) -> None:
    wire_text = json.dumps(entry(1) | fields)

    with pytest.raises(ValidationError):
        PlaceEntry.model_validate_json(wire_text)


@pytest.mark.parametrize(
    "fields",
    [
        {"generation": ""},
        {"starts_at": 0},
        {"starts_at": 1.0},
        {"entries": [entry(1), entry(1)], "starts_at": 1},
        {"entries": [entry(2), entry(1)], "starts_at": 2},
        {"entries": [entry(2)], "starts_at": 1},
        {"entries": [entry(1), entry(2)], "starts_at": 2},
    ],
    ids=[
        "empty-generation",
        "zero-start",
        "fractional-start",
        "repeated-seq",
        "descending-seq",
        "first-seq-not-start",
        "start-after-first-seq",
    ],
)
def test_a_window_breaking_a_construction_rule_is_refused(fields: dict[str, Any]) -> None:
    wire = {"generation": "g", "starts_at": 1, "entries": [entry(1)]} | fields

    with pytest.raises(ValidationError):
        window_from(wire)


@pytest.mark.parametrize("place", [PLACE, *HOSTILE_PLACES])
def test_a_place_window_uri_round_trips_to_its_place(place: PlaceRef) -> None:
    uri = place_window_uri(place)

    assert uri.startswith(f"{STANDING_URI}/places/")
    assert place_ref_from_window_uri(uri) == place


@pytest.mark.parametrize("place", HOSTILE_PLACES)
def test_a_place_window_uri_has_exactly_two_segments_below_places(place: PlaceRef) -> None:
    uri = place_window_uri(place)

    assert len(uri.removeprefix(f"{STANDING_URI}/places/").split("/")) == 2


@pytest.mark.parametrize(
    "uri",
    [
        "other://standing/places/room/r-1",
        "member://standing",
        "member://standing/",
        "member://standing/places",
        "member://standing/places/",
        "member://standing/places/room",
        "member://standing/places/room/",
        "member://standing/places//r-1",
        "member://standing/places/room/r-1/more",
        "member://standing/places/room/a/b",
        "member://standing/other/room/r-1",
        "member://standing/places/ro%6Fm/r-1",
        "member://standing/places/room/r%2d1",
        "member://standing/places/room/r 1",
        "member://standing/places/room/%zz",
        "member://standing/places/room/%",
        "member://standing/places/room/%ff",
        "member://standing/places/room/%C3",
        "member://standing/places/room/r-1?x=1",
        "member://standing/places/room/r-1#f",
        "member://standing/places/\ud800/b",
        "member://standing/places/a/\ud800",
        "member://standing/places/\ud800%41/b",
        "",
        None,
    ],
)
def test_a_uri_that_is_not_the_canonical_place_window_uri_yields_no_place(uri: str | None) -> None:
    assert place_ref_from_window_uri(uri) is None  # type: ignore[arg-type]


def test_accept_invitation_arguments_take_the_place_straight_from_the_standing() -> None:
    standing = Standing(place=PLACE, state=StandingState.INVITED, invitation=INVITATION)
    assert standing.invitation is not None

    arguments = AcceptInvitationArguments(
        place=standing.place, invitation=standing.invitation.id, epoch=standing.invitation.epoch
    )

    assert json.loads(arguments.model_dump_json()) == {
        "place": {"kind": "room", "id": "r-1"},
        "invitation": "i-1",
        "epoch": 3,
    }
    assert AcceptInvitationArguments.model_validate_json(arguments.model_dump_json()) == arguments


@pytest.mark.parametrize(
    "fields",
    [{"epoch": -1}, {"epoch": 1.0}, {"invitation": ""}, {"extra": 1}],
    ids=["negative-epoch", "fractional-epoch", "empty-invitation", "unknown-member"],
)
def test_accept_invitation_arguments_with_a_malformed_field_are_refused(
    fields: dict[str, Any],
) -> None:
    wire = {"place": {"kind": "room", "id": "r-1"}, "invitation": "i-1", "epoch": 3} | fields

    wire_text = json.dumps(wire)

    with pytest.raises(ValidationError):
        AcceptInvitationArguments.model_validate_json(wire_text)


def test_speak_arguments_carry_the_place_and_one_sentence_on_the_wire() -> None:
    arguments = SpeakArguments(place=PLACE, text="Shall we start?")

    assert json.loads(arguments.model_dump_json()) == speak_wire()
    assert SpeakArguments.model_validate_json(arguments.model_dump_json()) == arguments


@pytest.mark.parametrize(
    "fields",
    [
        {"text": ""},
        {"extra": 1},
        {"place": {"kind": "room"}},
        {"place": {"kind": "room", "id": ""}},
        {"place": "room/r-1"},
    ],
    ids=["empty-text", "unknown-member", "place-without-id", "place-with-empty-id", "text-place"],
)
def test_speak_arguments_with_a_malformed_field_are_refused(fields: dict[str, Any]) -> None:
    wire_text = json.dumps(speak_wire(**fields))

    with pytest.raises(ValidationError):
        SpeakArguments.model_validate_json(wire_text)


def test_spoken_reads_the_sound_length_in_milliseconds_from_its_json() -> None:
    spoken = Spoken.model_validate_json(json.dumps({"duration_ms": 1}))

    assert spoken.duration_ms == 1


@pytest.mark.parametrize(
    "fields",
    [{"duration_ms": 0}, {"duration_ms": 1.0}, {"extra": 1}],
    ids=["zero-duration", "fractional-duration", "unknown-member"],
)
def test_spoken_with_a_malformed_field_is_refused(fields: dict[str, Any]) -> None:
    wire_text = json.dumps({"duration_ms": 1} | fields)

    with pytest.raises(ValidationError):
        Spoken.model_validate_json(wire_text)


REFUSALS = [
    pytest.param(SpeakRefused, SpeakRefusal, ("not_active", "too_long", "unavailable"), id="speak"),
    pytest.param(
        AcceptInvitationRefused,
        AcceptInvitationRefusal,
        ("stale_invitation", "two_seats", "sponsor_away", "busy"),
        id="accept-invitation",
    ),
]


@pytest.mark.parametrize(
    "from_json_text", [True, False], ids=["from-json-text", "from-decoded-json"]
)
@pytest.mark.parametrize(("record", "codes_enum", "codes"), REFUSALS)
def test_a_refused_content_carries_exactly_its_tools_refusal_codes(
    record: Any, codes_enum: Any, codes: tuple[str, ...], from_json_text: bool
) -> None:
    wires = [{"refused": code} for code in codes]

    contents = [
        record.model_validate_json(json.dumps(wire))
        if from_json_text
        else record.model_validate(wire)
        for wire in wires
    ]

    assert {content.refused for content in contents} == set(codes_enum)
    assert [json.loads(content.model_dump_json()) for content in contents] == wires


ONE_CODE_EACH = [
    pytest.param(record, codes[0], id=refusal.id)
    for refusal in REFUSALS
    for record, _codes_enum, codes in [refusal.values]
]


@pytest.mark.parametrize(("record", "code"), ONE_CODE_EACH)
@pytest.mark.parametrize(
    "malformed",
    [
        lambda _code: json.dumps({"refused": "too_fast"}),
        lambda _code: json.dumps({"refused": "own_account_only"}),
        lambda code: json.dumps({"refused": code.upper()}),
        lambda code: json.dumps({"refused": code, "extra": 1}),
        lambda _code: json.dumps({}),
        lambda code: json.dumps(code),
        lambda code: code,
    ],
    ids=[
        "other-code",
        "audience-code",
        "upper-case-code",
        "unknown-member",
        "no-code",
        "bare-code",
        "bare-text",
    ],
)
def test_a_malformed_refused_content_is_refused(
    record: Any, code: str, malformed: Callable[[str], str]
) -> None:
    wire = malformed(code)
    with pytest.raises(ValidationError):
        record.model_validate_json(wire)
