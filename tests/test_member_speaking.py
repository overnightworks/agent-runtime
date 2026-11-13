"""What an agent and an app say about speaking: style, stopping and being talked over."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from agents.member import (
    STOP_SPEAKING_TOOL,
    PlaceRef,
    PlaceWindow,
    SpeakAnswer,
    SpeakArguments,
    Spoken,
    StoppedSpeaking,
    StopSpeakingArguments,
    TalkedOver,
    VoiceState,
    Written,
)
from tests.support.member_wire import (
    entry,
    speak_wire,
    spoken_wire,
    talked_over_wire,
    talked_window_wire,
    window_from,
    written_wire,
)

PLACE = PlaceRef(kind="room", id="r-1")


@pytest.mark.parametrize(
    ("field", "value"),
    [("language", "en"), ("style", "louder and angrier")],
    ids=["language", "style"],
)
def test_speak_arguments_name_the_optional_field_of_the_sentence_when_given(
    field: str, value: str
) -> None:
    arguments = SpeakArguments(place=PLACE, text="Shall we start?", **{field: value})

    assert json.loads(arguments.model_dump_json()) == speak_wire(**{field: value})
    assert SpeakArguments.model_validate_json(arguments.model_dump_json()) == arguments


@pytest.mark.parametrize("field", ["language", "style"])
def test_speak_arguments_without_an_optional_field_dump_without_its_key(field: str) -> None:
    arguments = SpeakArguments.model_validate_json(json.dumps(speak_wire()))

    assert getattr(arguments, field) is None
    assert field not in json.loads(arguments.model_dump_json())


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
    wire = talked_window_wire(talked_over=talked_over_wire(**fields))

    with pytest.raises(ValidationError):
        window_from(wire)


def test_a_talked_over_record_without_an_id_is_refused() -> None:
    wire = talked_over_wire()
    del wire["id"]

    with pytest.raises(ValidationError):
        TalkedOver.model_validate_json(json.dumps(wire))


def test_a_partial_is_no_entry_and_the_seq_rules_hold_beside_it() -> None:
    window = window_from(talked_window_wire(entries=[entry(3), entry(5)], starts_at=3))

    assert [line.seq for line in window.entries] == [3, 5]
    out_of_order = talked_window_wire(entries=[entry(2), entry(1)], starts_at=2)
    with pytest.raises(ValidationError):
        window_from(out_of_order)


@pytest.mark.parametrize(
    ("wire", "record"),
    [(spoken_wire(), Spoken), (written_wire(), Written)],
    ids=["spoken", "written"],
)
def test_a_speak_answer_reads_back_from_its_json_as_the_marker_alone(
    wire: dict[str, Any], record: type[Spoken | Written]
) -> None:
    answer = record.model_validate_json(json.dumps(wire))

    assert json.loads(answer.model_dump_json()) == wire


@pytest.mark.parametrize("record", [Spoken, Written])
@pytest.mark.parametrize(
    "marker",
    [{"false": False}, {"text": "yes"}, {"number": 1}, {"empty": None}],
    ids=lambda marker: next(iter(marker)),
)
def test_a_speak_answer_whose_marker_is_not_exactly_true_is_refused(
    record: type[Spoken | Written], marker: dict[str, Any]
) -> None:
    wire = {record.__name__.lower(): next(iter(marker.values()))}

    with pytest.raises(ValidationError):
        record.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(
    ("record", "wire"),
    [
        (Spoken, spoken_wire(extra=1)),
        (Spoken, {"duration_ms": 1200}),
        (Spoken, spoken_wire(duration_ms=1200)),
        (Spoken, {}),
        (Written, written_wire(extra=1)),
        (Written, {}),
    ],
    ids=[
        "spoken-extra",
        "spoken-length-only",
        "spoken-with-length",
        "spoken-empty",
        "written-extra",
        "written-empty",
    ],
)
def test_a_speak_answer_with_an_unknown_or_missing_field_is_refused(
    record: type[Spoken | Written], wire: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        record.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(
    ("wire", "record"),
    [(spoken_wire(), Spoken), (written_wire(), Written)],
    ids=["spoken", "written"],
)
def test_a_speak_answer_reads_spoken_and_written_each_as_its_own_record(
    wire: dict[str, Any], record: type[Spoken | Written]
) -> None:
    answer = TypeAdapter(SpeakAnswer).validate_json(json.dumps(wire))

    assert type(answer) is record


@pytest.mark.parametrize(
    "wire",
    [spoken_wire() | written_wire(), {}],
    ids=["both", "neither"],
)
def test_a_speak_answer_that_is_both_or_neither_is_refused(wire: dict[str, Any]) -> None:
    text = json.dumps(wire)
    adapter = TypeAdapter(SpeakAnswer)

    with pytest.raises(ValidationError):
        adapter.validate_json(text)


@pytest.mark.parametrize("state", [VoiceState.AVAILABLE, VoiceState.UNAVAILABLE])
def test_a_window_reads_its_voice_state_as_sent(state: VoiceState) -> None:
    window = window_from(talked_window_wire(voice_state=state.value))

    assert window.voice_state is state
    assert json.loads(window.model_dump_json())["voice_state"] == state.value
    assert PlaceWindow.model_validate_json(window.model_dump_json()) == window


def test_a_window_without_a_voice_state_dumps_without_the_key() -> None:
    window = PlaceWindow(generation="g", starts_at=1, entries=())

    assert window.voice_state is None
    assert "voice_state" not in json.loads(window.model_dump_json())


@pytest.mark.parametrize("state", ["down", True, ""], ids=["unknown-word", "bool", "empty"])
def test_a_window_with_a_malformed_voice_state_is_refused(state: object) -> None:
    wire = talked_window_wire(voice_state=state)

    with pytest.raises(ValidationError):
        window_from(wire)


def test_a_cut_spoken_line_reads_back_from_its_json_with_its_mark() -> None:
    window = window_from(
        talked_window_wire(entries=[entry(1, own=True, spoken=True, cut=True, text="Shall we")])
    )

    assert window.entries[0].cut is True
    assert PlaceWindow.model_validate_json(window.model_dump_json()) == window


def test_a_line_that_is_not_cut_dumps_without_the_mark_and_reads_it_as_absent() -> None:
    window = window_from(talked_window_wire(entries=[entry(1, spoken=True)]))

    assert window.entries[0].cut is False
    assert "cut" not in json.loads(window.model_dump_json())["entries"][0]


def test_a_cut_mark_on_a_written_line_is_refused() -> None:
    wire = talked_window_wire(entries=[entry(1, spoken=False, cut=True)])

    with pytest.raises(ValidationError):
        window_from(wire)


@pytest.mark.parametrize("cut", ["yes", 1], ids=["text", "number"])
def test_an_entry_with_a_malformed_cut_mark_is_refused(cut: object) -> None:
    wire = talked_window_wire(entries=[entry(1, spoken=True, cut=cut)])

    with pytest.raises(ValidationError):
        window_from(wire)
