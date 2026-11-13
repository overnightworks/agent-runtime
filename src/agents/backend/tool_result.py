"""Running one tool call of a turn: its handler's outcome as the result event the model is shown.

A handler is the app's code, so what it raises or answers invalidly is a host defect that fails
the call and not the turn. This module is the one owner of that bound; it logs the call's name,
id and error flag, never its arguments or content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from agents.backend import TurnPerson, TurnStopped
from agents.tools import HandToolHandler, JsonObject, ToolOutcome
from agents.turn import MAX_EVENT_BYTES
from agents.turn_events import ToolResultEvent

__all__ = ["RequestedCall", "serve"]

_LOGGER: Final = logging.getLogger(__name__)
_HANDLER_FAILED_TEXT: Final = "The tool failed."
_RESULT_TOO_LARGE_TEXT: Final = "The tool result was too large to return."


@dataclass(frozen=True)
class RequestedCall:
    id: str
    name: str
    arguments: JsonObject


async def serve(
    handler: HandToolHandler, person: TurnPerson, call: RequestedCall
) -> ToolResultEvent:
    """Run one handler; an outcome that is no valid result event is a host defect like a raise.

    The one raise that is no defect is ``TurnStopped``: the handler ends the turn, so it passes.
    """
    # unset for a call the deadline, a cancellation or a handler's TurnStopped cut off
    is_error: bool | None = None
    try:
        event = _result_event(call, await handler(person.principal, call.arguments))
    except TurnStopped:
        raise
    except Exception as error:  # a handler is the app's code; its failure is a host defect
        _LOGGER.error("tool %s raised %s", call.name, type(error).__qualname__)
        event = _failed_event(call, _HANDLER_FAILED_TEXT)
        is_error = True
        return event
    else:
        is_error = event.is_error
        return event
    finally:
        _LOGGER.info("tool %s call %r ended, is_error=%s", call.name, call.id, is_error)


def _result_event(call: RequestedCall, outcome: ToolOutcome) -> ToolResultEvent:
    event = ToolResultEvent.model_validate(
        {"tool_use_id": call.id, "content": outcome.content, "is_error": outcome.is_error},
        strict=True,
    )
    if len(event.content.encode("utf-8")) > MAX_EVENT_BYTES:
        return _failed_event(call, _RESULT_TOO_LARGE_TEXT)
    return event


def _failed_event(call: RequestedCall, text: str) -> ToolResultEvent:
    return ToolResultEvent(tool_use_id=call.id, content=text, is_error=True)
