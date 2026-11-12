"""The event family a provider turn streams to its in-process caller.

Every backend yields these types. They are in-process events and no wire format.
Their field names and defaults are a public contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents.turn import RunFile, TurnUsage


class StreamEvent(BaseModel):
    """Base class for every event a provider turn streams."""

    type: str


class AssistantTextEvent(StreamEvent):
    """One piece of the assistant's text, yielded as the gateway streams it.

    A piece shows before its round is judged. Pieces come from every round, including a round
    that ends in tool calls, whose pieces come before its ``ToolCallEvent``s. The last round's
    pieces, joined, are ``FinalEvent.text``. A turn that ends with ``TurnStopped`` may have
    yielded pieces first; nothing withdraws them.
    """

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
    """A turn's ending: its words, the files it produced and the tokens it used.

    ``text`` is the last round's judged text only, so when tools ran, the turn's pieces joined
    need not be ``text``. A picture turn ends with its one picture in ``files`` and empty
    ``text``; a text turn leaves ``files`` empty.

    ``usage`` is the sum of the usage of every round of a text turn. It is ``None`` when any
    round's usage is missing or unreadable, never a partial sum, and for a picture turn. A turn
    that stops ends with no final event and so reports none.
    """

    type: Literal["final"] = "final"
    text: str
    files: tuple[RunFile, ...] = ()
    usage: TurnUsage | None = None
