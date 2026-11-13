"""``ToolImage``: the rules of the one picture a tool hands an agent, and its JSON form."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.turn import RunFile, ToolImage
from agents.turn_events import ToolResultEvent
from tests.support.tool_images import a_tool_image


@pytest.mark.parametrize(
    "content",
    [b"", b"\x89PNG\r\n\x1a\n", b"\xff\xd8", b"GIF89a\xff\xd8\xff"],
    ids=["empty", "a png", "a truncated start marker", "a marker after the start"],
)
def test_a_tool_image_refuses_bytes_that_are_no_jpeg(content: bytes) -> None:
    with pytest.raises(ValidationError):
        ToolImage(content=content)


def test_a_tool_image_takes_a_jpeg_of_any_size_past_its_marker() -> None:
    assert ToolImage(content=a_tool_image(3).content).content == a_tool_image(3).content


def test_a_result_event_with_an_image_round_trips_through_json_as_canonical_base64() -> None:
    event = ToolResultEvent(tool_use_id="c1", content="seen", image=a_tool_image())

    json_form = event.model_dump_json()

    assert ToolResultEvent.model_validate_json(json_form) == event
    assert ToolResultEvent.model_validate_json(json_form).image == a_tool_image()


def test_a_result_event_without_an_image_has_none() -> None:
    assert ToolResultEvent(tool_use_id="c1", content="seen").image is None


CANONICAL_JPEG_START = "/9j/AA=="


@pytest.mark.parametrize(
    "spelling",
    [" /9j/AA==", "/9j/AA==\n", "_9j_AA==", "/9j/AA", "/9j/AB=="],
    ids=["leading space", "trailing newline", "url-safe", "no padding", "stray bits"],
)
@pytest.mark.parametrize("record", ["file", "image"])
def test_a_record_of_bytes_refuses_a_spelling_that_is_not_canonical_base64(
    record: str, spelling: str
) -> None:
    def read(content: str) -> object:
        if record == "file":
            return RunFile.model_validate_json(f'{{"name": "n", "content": "{content}"}}')
        return ToolImage.model_validate_json(f'{{"content": "{content}"}}')

    assert read(CANONICAL_JPEG_START) is not None
    with pytest.raises(ValidationError):
        read(spelling)
