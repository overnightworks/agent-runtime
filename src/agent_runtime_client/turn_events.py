"""The event family a provider turn streams to its in-process caller.

Every backend yields these types. They are in-process events and no wire format.
Their field names and defaults are a public contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_runtime_client.turn import RunFile


class StreamEvent(BaseModel):
    """Base class for every event a provider turn streams."""

    type: str


class AssistantTextEvent(StreamEvent):
    type: Literal["assistant_text"] = "assistant_text"
    text: str


class ToolCallEvent(StreamEvent):
    type: Literal["tool_call"] = "tool_call"
    tool_use_id: str
    name: str
    input: dict = Field(default_factory=dict)


class ToolResultEvent(StreamEvent):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class FinalEvent(StreamEvent):
    """A turn's ending: its words and the files it produced.

    A CLI run collects the files it left in its output folder, and a gateway picture turn ends
    with its one picture; a gateway text turn leaves ``files`` empty.
    """

    type: Literal["final"] = "final"
    text: str
    files: tuple[RunFile, ...] = ()
