"""The tool contract an app declares and every agent layer reads.

This module is the one owner of ``ToolDeclaration`` and its ``ToolEffect``. ``HandTool`` is a
declaration with the handler an app serves it with, for the person a turn acts for;
``ToolOutcome`` is what a handler answers. ``Principal`` and ``JsonObject`` are re-exported so an
app's tools import only this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime_client.turn import JsonObject, Principal

__all__ = [
    "HandTool",
    "HandToolHandler",
    "JsonObject",
    "Principal",
    "ToolDeclaration",
    "ToolEffect",
    "ToolOutcome",
    "agent_tool",
    "not_an_agent_tool",
]

AGENT_TOOL_EXTENSION: Final = "x-agent-tool"


class ToolEffect(StrEnum):
    """Whether running a tool can change state outside the run.

    The effect class decides ``TurnStopped.after_side_effect``: a turn that ended
    after a side-effecting call started tells the app it may have written.
    """

    READ_ONLY = "read_only"
    SIDE_EFFECTING = "side_effecting"


class ToolDeclaration(BaseModel):
    """One tool as a model sees it: its name, its purpose, its arguments.

    ``effect`` is for the host, not the model: ``GatewayBackend`` reads it for
    ``after_side_effect``, and the gateway projection picks its fields explicitly, so it never
    reaches a prompt. It defaults to side-effecting because a tool nobody classified must be
    assumed to change state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict[str, Any]
    effect: ToolEffect = ToolEffect.SIDE_EFFECTING


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool answers: the text a model is shown and whether the call failed."""

    content: str
    is_error: bool


type HandToolHandler = Callable[[Principal, JsonObject], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class HandTool:
    """A tool the app serves itself, for the person a turn acts for."""

    declaration: ToolDeclaration
    handler: HandToolHandler


def agent_tool(*, name: str, effect: ToolEffect) -> dict[str, object]:
    """The mark that makes a route a tool: pass it as FastAPI's ``openapi_extra``.

    A route becomes a tool only through its own mark; the other decision is
    ``not_an_agent_tool``. The client imports no FastAPI.
    """
    return {AGENT_TOOL_EXTENSION: {"name": name, "effect": effect.value}}


def not_an_agent_tool(*, reason: str) -> dict[str, object]:
    """The exclusion of a route, with its reason: pass it as FastAPI's ``openapi_extra``.

    An excluded route yields no tool and no mark. The generator reads the same extension key as
    for ``agent_tool``, so one route cannot hold both decisions, and it validates the reason.
    """
    return {AGENT_TOOL_EXTENSION: {"excluded": reason}}
