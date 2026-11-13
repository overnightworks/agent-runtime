"""The person front answers a tool's image beside or instead of its text, within the event bound."""

from __future__ import annotations

import asyncio
import base64

import pytest

from agents.mcp_server import McpPersonFront
from agents.tools import ToolEffect, ToolOutcome
from agents.turn import MAX_EVENT_BYTES, TOOL_IMAGE_MEDIA_TYPE
from tests.support.mcp_person_support import Person, call_read_note, post
from tests.support.mcp_server_support import call_params, rpc_body
from tests.support.tool_images import a_tool_image


@pytest.fixture
def person() -> Person:
    return Person()


@pytest.fixture
def front(person: Person) -> McpPersonFront:
    tools = [person.tool("read_note"), person.tool("edit_note", ToolEffect.SIDE_EFFECTING)]
    return McpPersonFront(tools, resolve_person=person.resolve)


def test_the_sdk_client_gets_an_image_result_as_an_image_block_after_its_text(
    front: McpPersonFront, person: Person
) -> None:
    image = a_tool_image()
    person.outcome = ToolOutcome("a doorway", is_error=False, image=image)

    result = asyncio.run(call_read_note(front))

    text_block, image_block = result.content
    assert (result.is_error, text_block.text) == (False, "a doorway")
    assert image_block.mime_type == TOOL_IMAGE_MEDIA_TYPE
    assert base64.b64decode(image_block.data, validate=True) == image.content


@pytest.mark.parametrize("blank", ["", "  \n\t "], ids=["empty", "whitespace only"])
@pytest.mark.parametrize("is_error", [False, True])
def test_the_sdk_client_gets_an_image_alone_when_its_text_is_blank(
    front: McpPersonFront, person: Person, blank: str, is_error: bool
) -> None:
    image = a_tool_image()
    person.outcome = ToolOutcome(blank, is_error=is_error, image=image)

    result = asyncio.run(call_read_note(front))

    (image_block,) = result.content
    assert (result.is_error, image_block.type) == (is_error, "image")
    assert base64.b64decode(image_block.data, validate=True) == image.content


@pytest.mark.parametrize("blank", ["", "  \n\t "], ids=["empty", "whitespace only"])
@pytest.mark.parametrize("is_error", [False, True])
def test_the_sdk_client_gets_the_blank_text_block_when_there_is_no_image(
    front: McpPersonFront, person: Person, blank: str, is_error: bool
) -> None:
    person.outcome = ToolOutcome(blank, is_error=is_error)

    result = asyncio.run(call_read_note(front))

    (text_block,) = result.content
    assert (result.is_error, text_block.type, text_block.text) == (is_error, "text", blank)


@pytest.mark.parametrize(
    ("image_bytes", "text_bytes", "reaches_the_client"),
    [
        (MAX_EVENT_BYTES - 10, 10, True),
        (MAX_EVENT_BYTES - 10, 11, False),
        (MAX_EVENT_BYTES, 0, True),
        (MAX_EVENT_BYTES + 1, 0, False),
    ],
    ids=[
        "text and image at the bound",
        "text and image one over",
        "an image alone at the bound",
        "an image alone one over",
    ],
)
def test_an_image_result_over_the_event_bound_is_replaced_by_a_fixed_error(
    front: McpPersonFront,
    person: Person,
    image_bytes: int,
    text_bytes: int,
    reaches_the_client: bool,
) -> None:
    person.outcome = ToolOutcome("a" * text_bytes, is_error=False, image=a_tool_image(image_bytes))

    result = post(front, rpc_body("tools/call", call_params("read_note"))).json()["result"]

    reached = ["image"] if text_bytes == 0 else ["text", "image"]
    assert [block["type"] for block in result["content"]] == (
        reached if reaches_the_client else ["text"]
    )
    assert result["isError"] is (not reaches_the_client)
    if not reaches_the_client:
        assert result["content"][0]["text"] == "the tool result was too large"
