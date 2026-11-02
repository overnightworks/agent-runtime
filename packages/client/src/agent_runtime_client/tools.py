"""The tool contract an app declares and every agent layer reads.

This module is the one owner of ``ToolDeclaration`` and its ``ToolEffect``;
the provider core builds its ``ToolCatalog`` on the declaration and
re-exports the identical class. ``HandTool`` is a declaration with the handler
that serves it in the capability front, whose caller is the person a run acts for.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    # ``wire`` imports this module, so the names its siblings own are read by checkers only.
    from agent_runtime_client.capability import CapabilityPrincipal
    from agent_runtime_client.mcp_wire import ToolOutcome
    from agent_runtime_client.wire import JsonObject


class ToolEffect(StrEnum):
    """Whether running a tool can change state outside the run.

    The effect class decides how a cut-short run ends: once a side-effecting
    call was emitted, a cancel or a disconnect is reported as ``failed:
    interrupted`` rather than ``cancelled`` (decision record 0001, H6).
    """

    READ_ONLY = "read_only"
    SIDE_EFFECTING = "side_effecting"


class ToolDeclaration(BaseModel):
    """One tool as a model sees it: its name, its purpose, its arguments.

    ``effect`` is for the service, not the model: the provider projections
    pick their fields explicitly, so it never reaches a prompt. It defaults to
    side-effecting because a tool nobody classified must be assumed to change
    state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict[str, Any]
    effect: ToolEffect = ToolEffect.SIDE_EFFECTING


type HandToolHandler = Callable[[CapabilityPrincipal, JsonObject], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class HandTool:
    """A tool the app serves itself, for the person a run acts for."""

    declaration: ToolDeclaration
    handler: HandToolHandler
