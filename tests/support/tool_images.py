"""Builders for the one still picture a tool hands an agent."""

from __future__ import annotations

from agents.turn import ToolImage

_JPEG_START = b"\xff\xd8\xff"


def a_tool_image(size: int = 64) -> ToolImage:
    """A JPEG of ``size`` bytes: the start marker the standard fixes, then filler."""
    return ToolImage(content=(_JPEG_START + bytes(range(256)) * size)[:size])
