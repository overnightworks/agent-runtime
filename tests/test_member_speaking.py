"""What an agent and an app say about speaking: style, stopping and being talked over."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from agents.member import (
    STOP_SPEAKING_TOOL,
    PartialSpeech,
    PlaceRef,
    PlaceWindow,
    SpeakArguments,
    StoppedSpeaking,
    StopSpeakingArguments,
    TalkedOver,
)
from tests.support.member_wire import (
    entry,
    speak_wire,
    talked_over_wire,
    talked_window_wire,
    window_from,
)

PLACE = PlaceRef(kind="room", id="r-1")


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
