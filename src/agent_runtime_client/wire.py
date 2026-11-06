"""The run contract between the agent service and its clients.

This module is the one owner of what travels between a client and the
service: the request that starts a run, the run's
event stream as a closed union discriminated on ``type`` with every event
carrying the protocol version, the run id and its sequence number, and the
typed body the service answers with when it refuses a request, the status
body it answers ``GET /runs/{id}`` with and the list of offered models it
answers ``GET /models`` with.
A new variant or a new field is a new ``PROTOCOL_VERSION``.

Validation is strict, so the wire's JSON types are the contract: a sequence
number sent as ``"3"`` is a mismatch, not a coercion. Only the run id and the
closed codes and roles are read from their JSON strings. The version and sequence
number have no defaults.
"""

from __future__ import annotations

import re
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal, Self, TypeGuard, get_args
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from agent_runtime_client.tools import ToolEffect
from agent_runtime_client.turn import (
    MAX_AGENT_LENGTH,
    MAX_EVENT_BYTES,
    MAX_RUN_FILE_NAME_BYTES,
    MAX_RUN_FILES_BYTES,
    MAX_TOOL_INPUT_DEPTH,
    MODEL_PART_PATTERN,
    AgentName,
    AgentRole,
    AgentRoles,
    FailureCode,
    JsonObject,
    NonEmptyUnicodeText,
    RejectionCode,
    RunFile,
    StrictModel,
    ToolUseId,
    Turn,
    TurnMessage,
    TurnRole,
    UnicodeText,
    base64_length,
    refuse_non_json_constant,
)

# Every name the turn module owns stays importable from here, spelled out because
# mypy and pyright honour only the explicit form as a re-export.
__all__ = [
    "MAX_AGENT_LENGTH",
    "MAX_EVENT_BYTES",
    "MAX_RUN_FILES_BYTES",
    "MAX_RUN_FILE_NAME_BYTES",
    "MAX_TOOL_INPUT_DEPTH",
    "MODEL_PART_PATTERN",
    "AgentName",
    "AgentRole",
    "AgentRoles",
    "FailureCode",
    "JsonObject",
    "NonEmptyUnicodeText",
    "RejectionCode",
    "RunFile",
    "ToolUseId",
    "Turn",
    "TurnMessage",
    "TurnRole",
    "UnicodeText",
    "base64_length",
    "refuse_non_json_constant",
]

PROTOCOL_VERSION: Final = 6
# Base64 pads each file separately, so the allowance for the files' contents is
# the ceiling's own base64 length; the frame rules live in ``sse``.
MAX_SUCCEEDED_FRAME_BYTES: Final = MAX_EVENT_BYTES + base64_length(MAX_RUN_FILES_BYTES)
MAX_MESSAGE_LENGTH: Final = 500
_MODEL_NAME_PATTERN: Final = re.compile(rf"[a-z0-9-]+/{MODEL_PART_PATTERN.pattern}")


class RouteKind(StrEnum):
    """Which kind of runner serves a run."""

    CLI = "cli"
    API = "api"


class RunState(StrEnum):
    """Where a run stands: still running, or how it ended."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ClientLoss(StrEnum):
    """How the service lost the client of a run: it fell behind, or it went away."""

    DROPPED = "dropped"
    DISCONNECTED = "disconnected"


def _json_integer(value: object) -> object:
    # A Literal ignores strict mode and matches by equality, so without this
    # JSON ``true`` and ``1.0`` would pass as 1 and ``false`` as 0.
    if type(value) is not int:
        raise ValueError("must be a JSON integer")
    return value


ProtocolVersion = Annotated[Literal[6], BeforeValidator(_json_integer)]
"""The one decider of whether a ``v`` is the version this package speaks."""


def _refuse_non_model_name(text: str) -> str:
    if _MODEL_NAME_PATTERN.fullmatch(text) is None:
        raise ValueError("must be <provider>/<model> with no version, such as anthropic/sonnet")
    return text


ModelName = Annotated[str, AfterValidator(_refuse_non_model_name)]
"""The one decider of a requested model: ``<provider>/<model>``, never a version."""


class _WireModel(StrictModel):
    v: ProtocolVersion


class _RunEvent(_WireModel):
    run_id: Annotated[UUID, Field(strict=False)]
    seq: int = Field(ge=0)


class RunStarted(_RunEvent):
    """The first event of every run: the selected model and route, and the heartbeat cadence."""

    type: Literal["run_started"] = "run_started"
    seq: Annotated[Literal[0], BeforeValidator(_json_integer)]
    model: ModelName
    route: Annotated[RouteKind, Field(strict=False)]
    heartbeat_interval_s: int = Field(ge=1, le=300)


class AssistantText(_RunEvent):
    type: Literal["assistant_text"] = "assistant_text"
    text: NonEmptyUnicodeText


class ToolCall(_RunEvent):
    """A tool the model asked for, emitted before the tool runs."""

    type: Literal["tool_call"] = "tool_call"
    tool_use_id: ToolUseId
    name: UnicodeText
    input: JsonObject
    effect: Annotated[ToolEffect, Field(strict=False)]


class ToolResult(_RunEvent):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: ToolUseId
    content: UnicodeText
    is_error: bool


class Heartbeat(_RunEvent):
    """Proof of life while a run is idle; it counts in ``seq`` like any event."""

    type: Literal["heartbeat"] = "heartbeat"


def _list_as_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class Succeeded(_RunEvent):
    """A run that ended with its words, its files, or both."""

    type: Literal["succeeded"] = "succeeded"
    text: UnicodeText
    files: Annotated[tuple[RunFile, ...], BeforeValidator(_list_as_tuple)]

    @model_validator(mode="after")
    def _refuse_unusable_result(self) -> Self:
        if not self.text and not self.files:
            raise ValueError("a run without files must have text")
        if len({file.name for file in self.files}) != len(self.files):
            raise ValueError("file names must be unique")
        if sum(len(file.content) for file in self.files) > MAX_RUN_FILES_BYTES:
            raise ValueError(f"files together exceed {MAX_RUN_FILES_BYTES} bytes")
        return self


class Failed(_RunEvent):
    """A run that ended without success.

    ``message`` is written by the service, never a provider's raw output.
    """

    type: Literal["failed"] = "failed"
    code: Annotated[FailureCode, Field(strict=False)]
    message: Annotated[UnicodeText, Field(max_length=MAX_MESSAGE_LENGTH)]


class Cancelled(_RunEvent):
    type: Literal["cancelled"] = "cancelled"


TerminalEvent = Succeeded | Failed | Cancelled

_RunEventVariant = (
    RunStarted | AssistantText | ToolCall | ToolResult | Heartbeat | Succeeded | Failed | Cancelled
)

RunEvent = Annotated[_RunEventVariant, Field(discriminator="type")]

_RUN_EVENT_TYPES: Final = frozenset(get_args(_RunEventVariant))


def is_run_event(value: object) -> TypeGuard[RunEvent]:
    # The exact type, not isinstance: a subclass may add fields the closed
    # union does not carry.
    return type(value) in _RUN_EVENT_TYPES


class RunRejection(_WireModel):
    """The HTTP body of a request the service refused."""

    code: Annotated[RejectionCode, Field(strict=False)]
    message: Annotated[UnicodeText, Field(max_length=MAX_MESSAGE_LENGTH)]


class ModelListEntry(StrictModel):
    """One model the service offers."""

    id: ModelName
    object: Literal["model"] = "model"


class ModelList(_WireModel):
    """The HTTP body of ``GET /models``: the models a run may name, sorted by id."""

    object: Literal["list"] = "list"
    data: list[ModelListEntry]


def _refuse_non_utc_instant(instant: AwareDatetime) -> AwareDatetime:
    if instant.utcoffset() != timedelta(0):
        raise ValueError("must be a UTC instant")
    return instant


UtcInstant = Annotated[AwareDatetime, AfterValidator(_refuse_non_utc_instant)]
"""The one decider of a wire timestamp: an aware instant written in UTC."""


class RunStatus(_WireModel):
    """The HTTP body of ``GET /runs/{id}``: where a run stands.

    ``events`` counts what the service accepted, not what the client received.
    ``ended_at`` may precede ``started_at`` when the service's clock stepped back.
    """

    run_id: Annotated[UUID, Field(strict=False)]
    state: Annotated[RunState, Field(strict=False)]
    failure_code: Annotated[FailureCode | None, Field(strict=False)]
    events: int = Field(ge=1)
    started_at: UtcInstant
    ended_at: UtcInstant | None
    client_lost: Annotated[ClientLoss | None, Field(strict=False)]

    @model_validator(mode="after")
    def _ending_matches_state(self) -> Self:
        if (self.failure_code is not None) != (self.state is RunState.FAILED):
            raise ValueError("failure_code is set exactly when the run failed")
        if (self.ended_at is None) != (self.state is RunState.RUNNING):
            raise ValueError("ended_at is set exactly when the run is over")
        return self


class RunRequest(_WireModel):
    """The HTTP body a client sends to start a run.

    ``tools`` maps each tool the run may call to its effect. The service signs
    exactly those names into the run's capability and stamps the effects onto
    the run's tool calls; it grants nothing else. ``model``, when named, must be one the
    service configuration offers; absent or null, the configured model runs. ``roles`` is
    what the agent file offers the agent to, never what the caller is: the
    service answers ``forbidden`` unless the person's own role is in it.
    """

    agent: AgentName
    turn: Turn
    roles: AgentRoles
    tools: dict[NonEmptyUnicodeText, Annotated[ToolEffect, Field(strict=False)]]
    model: ModelName | None = None

    @model_serializer(mode="wrap")
    def _omit_the_unnamed_model(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        body = handler(self)
        if body.get("model") is None:
            body.pop("model", None)
        return body
