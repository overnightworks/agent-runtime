"""The ``act`` marker of a place's line: a written line that says what someone did."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agents.member import PlaceEntry, PlaceWindow
from tests.support.voice_lines import a_line


def an_act() -> PlaceEntry:
    return a_line(1, "Ada plays", kind="move", spoken=False, act=True)


def test_an_act_line_survives_the_wire_with_its_marker() -> None:
    window = PlaceWindow(generation="g1", starts_at=1, entries=(an_act(),))

    read = PlaceWindow.model_validate_json(window.model_dump_json())

    assert read == window
    assert read.entries[0].act is True


@pytest.mark.parametrize("spoken", [False, True])
def test_a_line_that_is_no_act_dumps_without_the_key(spoken: bool) -> None:
    dumped = json.loads(a_line(1, "Ada says", spoken=spoken).model_dump_json())

    assert "act" not in dumped


@pytest.mark.parametrize("fields", [{}, {"cut": True}], ids=["spoken", "spoken and cut"])
def test_a_spoken_line_is_refused_with_act(fields: dict[str, bool]) -> None:
    with pytest.raises(ValidationError):
        PlaceEntry.model_validate({**an_act().model_dump(), "spoken": True, **fields})
