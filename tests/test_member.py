"""What an app and an agent say to each other about a member: records, window and URIs."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import AnyHttpUrl, ValidationError

from agents.member import (
    LEASE_SECONDS,
    LISTEN_BOUND_SECONDS,
    STANDING_URI,
    STOP_SPEAKING_TOOL,
    AcceptInvitationRefusal,
    AcceptInvitationRefused,
    DeclineInvitationRefusal,
    DeclineInvitationRefused,
    Invitation,
    MemberStanding,
    ParentPlace,
    PartialSpeech,
    PlaceEntry,
    PlaceRef,
    PlaceView,
    PlaceWindow,
    SpeakArguments,
    SpeakRefusal,
    SpeakRefused,
    Spoken,
    Standing,
    StandingState,
    StoppedSpeaking,
    StopSpeakingArguments,
    StopSpeakingRefusal,
    StopSpeakingRefused,
    TalkedOver,
    place_ref_from_view_uri,
    place_ref_from_window_uri,
    place_view_uri,
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


def talked_over_wire(**fields: Any) -> dict[str, Any]:
    return {
        "id": "t-1",
        "playing": True,
        "speech": [{"name": "ada", "text": "wait"}],
    } | fields


def talked_window_wire(**fields: Any) -> dict[str, Any]:
    return {
        "generation": "g",
        "starts_at": 1,
        "entries": [entry(1)],
        "talked_over": talked_over_wire(),
    } | fields


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


def test_speak_arguments_carry_the_place_and_one_sentence_on_the_wire() -> None:
    arguments = SpeakArguments(place=PLACE, text="Shall we start?")

    assert json.loads(arguments.model_dump_json()) == speak_wire()
    assert SpeakArguments.model_validate_json(arguments.model_dump_json()) == arguments


def test_speak_arguments_name_the_language_of_the_sentence_when_given() -> None:
    arguments = SpeakArguments(place=PLACE, text="Shall we start?", language="en")

    assert json.loads(arguments.model_dump_json()) == speak_wire(language="en")
    assert SpeakArguments.model_validate_json(arguments.model_dump_json()) == arguments


def test_speak_arguments_without_a_language_dump_without_its_key() -> None:
    arguments = SpeakArguments.model_validate_json(json.dumps(speak_wire()))

    assert arguments.language is None
    assert "language" not in json.loads(arguments.model_dump_json())


@pytest.mark.parametrize(
    "language",
    ["DE", "deu", "de-DE", "d", "d1", "", " de", 1, ["de"]],
    ids=[
        "uppercase",
        "three-letters",
        "region-tag",
        "one-letter",
        "digit",
        "empty",
        "padded",
        "number",
        "list",
    ],
)
def test_speak_arguments_with_a_malformed_language_are_refused(language: object) -> None:
    wire_text = json.dumps(speak_wire(language=language))

    with pytest.raises(ValidationError):
        SpeakArguments.model_validate_json(wire_text)


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


def test_speak_arguments_name_the_style_of_the_sentence_when_given() -> None:
    arguments = SpeakArguments(place=PLACE, text="Shall we start?", style="louder and angrier")

    assert json.loads(arguments.model_dump_json()) == speak_wire(style="louder and angrier")
    assert SpeakArguments.model_validate_json(arguments.model_dump_json()) == arguments


def test_speak_arguments_without_a_style_dump_without_its_key() -> None:
    arguments = SpeakArguments.model_validate_json(json.dumps(speak_wire()))

    assert arguments.style is None
    assert "style" not in json.loads(arguments.model_dump_json())


@pytest.mark.parametrize("style", ["", 1, ["calm"]], ids=["empty", "number", "list"])
def test_speak_arguments_with_a_malformed_style_are_refused(style: object) -> None:
    wire_text = json.dumps(speak_wire(style=style))

    with pytest.raises(ValidationError):
        SpeakArguments.model_validate_json(wire_text)


def test_stop_speaking_arguments_carry_the_place_on_the_wire() -> None:
    arguments = StopSpeakingArguments(place=PLACE)

    assert json.loads(arguments.model_dump_json()) == {"place": {"kind": "room", "id": "r-1"}}
    assert StopSpeakingArguments.model_validate_json(arguments.model_dump_json()) == arguments
    assert STOP_SPEAKING_TOOL == "stop_speaking"


@pytest.mark.parametrize(
    "wire",
    [
        {},
        {"place": "room/r-1"},
        {"place": {"kind": "room"}},
        {"place": {"kind": "room", "id": "r"}, "extra": 1},
    ],
    ids=["no-place", "text-place", "place-without-id", "unknown-member"],
)
def test_stop_speaking_arguments_with_a_malformed_field_are_refused(wire: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StopSpeakingArguments.model_validate_json(json.dumps(wire))


def test_stopped_speaking_names_the_sentence_it_cut() -> None:
    stopped = StoppedSpeaking.model_validate_json(json.dumps({"cut": "Shall we start?"}))

    assert stopped.cut == "Shall we start?"


@pytest.mark.parametrize(
    "wire",
    [{}, {"cut": ""}, {"cut": 1}, {"cut": "x", "dropped": 2}],
    ids=["no-cut", "empty-cut", "number-cut", "unknown-member"],
)
def test_stopped_speaking_with_a_malformed_field_is_refused(wire: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StoppedSpeaking.model_validate_json(json.dumps(wire))


def test_a_window_without_talked_over_dumps_without_the_key() -> None:
    window = PlaceWindow(generation="g", starts_at=1, entries=())

    assert window.talked_over is None
    assert "talked_over" not in json.loads(window.model_dump_json())


def test_a_talked_over_window_reads_back_from_its_json_unchanged() -> None:
    window = window_from(
        talked_window_wire(
            talked_over=talked_over_wire(
                playing=False,
                speech=[{"name": "ada", "text": ""}, {"name": "bob", "text": "no, stop"}],
            )
        )
    )

    assert window.talked_over is not None
    assert [speech.name for speech in window.talked_over.speech] == ["ada", "bob"]
    assert PlaceWindow.model_validate_json(window.model_dump_json()) == window


def test_a_partial_may_be_empty_while_a_person_has_said_no_words_yet() -> None:
    partial = PartialSpeech.model_validate_json(json.dumps({"name": "ada", "text": ""}))

    assert partial.text == ""


@pytest.mark.parametrize(
    "fields",
    [
        {"id": ""},
        {"playing": "yes"},
        {"speech": []},
        {"speech": [{"name": "", "text": "x"}]},
        {"speech": [{"name": "ada"}]},
        {"speech": [{"name": "ada", "text": "x", "extra": 1}]},
        {"extra": 1},
    ],
    ids=[
        "empty-id",
        "text-playing",
        "nobody-speaking",
        "empty-name",
        "no-text",
        "unknown-partial-member",
        "unknown-member",
    ],
)
def test_a_malformed_talked_over_record_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        window_from(talked_window_wire(talked_over=talked_over_wire(**fields)))


def test_a_talked_over_record_without_an_id_is_refused() -> None:
    wire = talked_over_wire()
    del wire["id"]

    with pytest.raises(ValidationError):
        TalkedOver.model_validate_json(json.dumps(wire))


def test_a_partial_is_no_entry_and_the_seq_rules_hold_beside_it() -> None:
    window = window_from(talked_window_wire(entries=[entry(3), entry(5)], starts_at=3))

    assert [line.seq for line in window.entries] == [3, 5]
    with pytest.raises(ValidationError):
        window_from(talked_window_wire(entries=[entry(2), entry(1)], starts_at=2))


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
    pytest.param(
        SpeakRefused,
        SpeakRefusal,
        (
            "not_active",
            "too_long",
            "unavailable",
            "language_unavailable",
            "style_unavailable",
            "stopped",
        ),
        id="speak",
    ),
    pytest.param(
        StopSpeakingRefused,
        StopSpeakingRefusal,
        ("not_active", "nothing_playing"),
        id="stop-speaking",
    ),
    pytest.param(
        AcceptInvitationRefused,
        AcceptInvitationRefusal,
        ("stale_invitation", "two_seats", "sponsor_away", "busy"),
        id="accept-invitation",
    ),
    pytest.param(
        DeclineInvitationRefused,
        DeclineInvitationRefusal,
        ("stale_invitation",),
        id="decline-invitation",
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


def test_a_place_view_reads_back_from_its_json_with_its_state_untouched() -> None:
    state = {
        "items": [{"label": "ä", "count": 2}, [None, 1.0, 1, True, 1.5]],
        "group": {"open": True},
        "note": None,
    }
    view = PlaceView(revision=4, awaits_you=True, state=state)

    read = PlaceView.model_validate_json(view.model_dump_json())

    assert read == view
    assert read.state == state
    assert json.dumps(read.state) == json.dumps(state)


@pytest.mark.parametrize(
    "fields",
    [
        {"state": {"when": datetime(2026, 11, 8, 12, tzinfo=UTC)}},
        {"state": {"bytes": b"x"}},
        {"state": {"nested": [{"set": {1}}]}},
        {"state": {"number": float("nan")}},
        {"state": {"lone": "\ud800"}},
        {"state": ["not", "an", "object"]},
        {"revision": -1},
        {"revision": 1.0},
        {"awaits_you": 1},
        {"extra": "field"},
    ],
    ids=[
        "datetime-in-state",
        "bytes-in-state",
        "set-in-nested-state",
        "nan-in-state",
        "lone-surrogate-in-state",
        "array-state",
        "negative-revision",
        "fractional-revision",
        "integer-awaits-you",
        "unknown-field",
    ],
)
def test_a_place_view_with_a_malformed_field_is_refused(fields: dict[str, Any]) -> None:
    valid = {"revision": 0, "awaits_you": False, "state": {}}

    with pytest.raises(ValidationError):
        PlaceView.model_validate(valid | fields)


@pytest.mark.parametrize("place", [PLACE, *HOSTILE_PLACES])
def test_a_place_view_uri_is_the_window_uri_and_a_view_segment_and_names_its_place(
    place: PlaceRef,
) -> None:
    uri = place_view_uri(place)

    assert uri == f"{place_window_uri(place)}/view"
    assert place_ref_from_view_uri(uri) == place


@pytest.mark.parametrize(
    "uri",
    [
        "member://standing/places/room/r-1",
        "member://standing/places/room/view",
        "member://standing/places/room/r-1/view/",
        "member://standing/places/room/r-1/views",
        "member://standing/places/room/r-1/View",
        "member://standing/places/room/r-1/view/more",
        "member://standing/places/room/r-1/view?x=1",
        "member://standing/places/room/r-1/view#f",
        "member://standing/places/room//view",
        "member://standing/places//r-1/view",
        "member://standing/places/ro%6Fm/r-1/view",
        "member://standing/places/room/r%2d1/view",
        "member://standing/places/room/%zz/view",
        "member://standing/places/room/%ff/view",
        "member://standing/places/\ud800/b/view",
        "member://standing/view",
        "/view",
        "view",
        "",
        None,
    ],
)
def test_a_uri_that_is_not_the_canonical_place_view_uri_yields_no_place(uri: str | None) -> None:
    assert place_ref_from_view_uri(uri) is None  # type: ignore[arg-type]


def test_a_place_named_view_has_a_window_uri_that_is_no_view_uri() -> None:
    place = PlaceRef(kind="room", id="view")

    assert place_ref_from_view_uri(place_window_uri(place)) is None
    assert place_ref_from_window_uri(place_view_uri(place)) is None


@pytest.mark.parametrize(
    "fields",
    [{}, {"serves_view": False}],
    ids=["unset", "false"],
)
def test_a_standing_that_does_not_serve_a_view_dumps_without_the_field(
    fields: dict[str, Any],
) -> None:
    standing = Standing(place=PLACE, state=StandingState.ACTIVE, **fields)

    assert standing.model_dump_json() == (
        '{"place":{"kind":"room","id":"r-1"},"state":"active","invitation":null}'
    )
    assert standing.model_dump() == {
        "place": {"kind": "room", "id": "r-1"},
        "state": StandingState.ACTIVE,
        "invitation": None,
    }


def test_a_standing_that_serves_a_view_reads_back_from_its_json() -> None:
    standing = Standing(place=PLACE, state=StandingState.ACTIVE, serves_view=True)

    dumped = standing.model_dump_json()

    assert json.loads(dumped)["serves_view"] is True
    assert Standing.model_validate_json(dumped) == standing


@pytest.mark.parametrize("serves_view", [1, "true", None])
def test_a_standing_whose_serves_view_is_no_boolean_is_refused(serves_view: object) -> None:
    with pytest.raises(ValidationError):
        Standing(place=PLACE, state=StandingState.ACTIVE, serves_view=serves_view)  # type: ignore[arg-type]


PARENT_JSON = {"app": "https://calls.example/mcp", "place": {"kind": "call", "id": "c-9"}}


def standing_json(state: str, **fields: Any) -> str:
    return json.dumps(
        {"place": {"kind": "room", "id": "r-1"}, "state": state, "invitation": None} | fields
    )


@pytest.mark.parametrize(
    "state",
    [StandingState.INVITED, StandingState.ACTIVE, StandingState.PAUSED],
)
@pytest.mark.parametrize("app", ["https://calls.example/mcp", "http://calls.example/mcp"])
def test_a_standing_with_a_parent_place_reads_back_from_its_json_in_every_state(
    state: StandingState, app: str
) -> None:
    parent = ParentPlace(app=AnyHttpUrl(app), place=PlaceRef(kind="call", id="c-9"))
    standing = Standing(
        place=PLACE,
        state=state,
        invitation=INVITATION if state is StandingState.INVITED else None,
        parent=parent,
    )

    dumped = standing.model_dump_json()

    assert json.loads(dumped)["parent"]["place"] == {"kind": "call", "id": "c-9"}
    assert Standing.model_validate_json(dumped) == standing


def test_a_standing_without_a_parent_place_dumps_without_the_key() -> None:
    standing = Standing.model_validate_json(standing_json("active"))

    assert standing.parent is None
    assert "parent" not in json.loads(standing.model_dump_json())
    assert "parent" not in standing.model_dump()


@pytest.mark.parametrize(
    "parent",
    [
        {"app": "/mcp", "place": PARENT_JSON["place"]},
        {"app": "calls.example/mcp", "place": PARENT_JSON["place"]},
        {"app": "ftp://calls.example/mcp", "place": PARENT_JSON["place"]},
        {"app": PARENT_JSON["app"], "place": {"kind": "", "id": "c-9"}},
        {"app": PARENT_JSON["app"], "place": {"kind": "call", "id": ""}},
        {**PARENT_JSON, "extra": 1},
        {"app": PARENT_JSON["app"]},
        {"place": PARENT_JSON["place"]},
    ],
    ids=[
        "relative-url",
        "no-scheme",
        "other-scheme",
        "empty-kind",
        "empty-id",
        "unknown-key",
        "no-place",
        "no-app",
    ],
)
def test_a_standing_whose_parent_place_is_malformed_is_refused(parent: dict[str, Any]) -> None:
    wire_text = standing_json("active", parent=parent)

    with pytest.raises(ValidationError):
        Standing.model_validate_json(wire_text)


def test_a_parent_app_equals_the_same_address_written_in_another_normal_form() -> None:
    plain = Standing.model_validate_json(standing_json("active", parent=PARENT_JSON))
    spelled = Standing.model_validate_json(
        standing_json("active", parent=PARENT_JSON | {"app": "https://CALLS.example:443/mcp"})
    )

    assert plain.parent is not None
    assert spelled.parent is not None
    assert plain.parent.app == spelled.parent.app


def test_the_parent_place_record_is_exported() -> None:
    import agents.member

    assert "ParentPlace" in agents.member.__all__
