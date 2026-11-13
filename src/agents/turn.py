"""The words an app uses for a turn: its conversation, files, tool images and failure codes.

This module is the one owner of the turn's vocabulary, shared by the backend, the tool contract
and the MCP person front. It imports neither ``tools`` nor ``mcp_wire``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
)


def base64_length(size: int) -> int:
    """The characters of the padded base64 of ``size`` bytes."""
    return 4 * -(-size // 3)


MAX_EVENT_BYTES: Final = 1_048_576
MAX_RUN_FILES_BYTES: Final = 33_554_432
MAX_RUN_FILE_NAME_BYTES: Final = 255
MAX_TOOL_INPUT_DEPTH: Final = 128
MAX_AGENT_LENGTH: Final = 64
MODEL_PART_PATTERN: Final = re.compile(r"[a-z0-9.-]+")
_AGENT_PATTERN: Final = re.compile(rf"[a-z][a-z0-9-]{{0,{MAX_AGENT_LENGTH - 1}}}")


class ModelKind(StrEnum):
    """What a gateway model makes: the name's prefix, and so the kind of agent that may run it."""

    TEXT = "text"
    IMAGE = "image"


_GATEWAY_MODEL_NAME: Final = re.compile(
    rf"(?:{'|'.join(map(re.escape, ModelKind))})/{MODEL_PART_PATTERN.pattern}"
)


class FailureCode(StrEnum):
    """Why a turn ended as failed."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"
    LIMIT_EXCEEDED = "limit_exceeded"


class RejectionCode(StrEnum):
    """Why a turn is refused."""

    NOT_ENABLED = "not_enabled"
    BUSY = "busy"
    BUDGET_SPENT = "budget_spent"
    FORBIDDEN = "forbidden"


class StopOutcome(StrEnum):
    """What a stopped turn means to a person; an app words only this, never the code."""

    NOT_ENABLED = "not_enabled"
    BUSY = "busy"
    BUDGET_SPENT = "budget_spent"
    UNAVAILABLE = "unavailable"


_REJECTION_OUTCOMES: Final = {
    RejectionCode.NOT_ENABLED: StopOutcome.NOT_ENABLED,
    RejectionCode.FORBIDDEN: StopOutcome.NOT_ENABLED,
    RejectionCode.BUSY: StopOutcome.BUSY,
    RejectionCode.BUDGET_SPENT: StopOutcome.BUDGET_SPENT,
}


def outcome_of_stop(code: RejectionCode | FailureCode) -> StopOutcome:
    """The one decider of what a stopped turn means to a person: every failure is unavailable."""
    if isinstance(code, RejectionCode):
        return _REJECTION_OUTCOMES[code]
    return StopOutcome.UNAVAILABLE


def refuse_non_json_constant(constant: str) -> object:
    """The one decider of NaN and Infinity in text being parsed: JSON has no word for them.

    Passed as ``parse_constant`` by the MCP request body reader,
    ``mcp_wire.read_strict_json``.
    """
    raise ValueError(f"{constant} is not a JSON value")


def _refuse_non_json_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    # pydantic has already refused a value that is no JSON value, a non-string key and a
    # self-containing value; what remains is the depth limit, which means depth never stops a
    # built input from encoding (pydantic's serializer refuses a value nested about 255 levels
    # deep), a number that is not finite and a string with a lone surrogate. The path holds each
    # open container's remaining children, so the walk's own bookkeeping never mixes with the
    # caller's values.
    path = [_children(value)]
    while path:
        for child in path[-1]:
            if isinstance(child, dict | list):
                if len(path) == MAX_TOOL_INPUT_DEPTH:
                    raise ValueError(
                        f"tool input nests deeper than {MAX_TOOL_INPUT_DEPTH} objects and arrays"
                    )
                path.append(_children(child))
                break
            _refuse_non_json_scalar(child)
        else:
            path.pop()
    return value


def _children(container: dict[str, JsonValue] | list[JsonValue]) -> Iterator[JsonValue]:
    if isinstance(container, dict):
        for key in container:
            _refuse_lone_surrogates(key)
        return iter(container.values())
    return iter(container)


def _refuse_non_json_scalar(value: str | int | float | bool | None) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value} is not a JSON number")
    elif isinstance(value, str):
        _refuse_lone_surrogates(value)


def _refuse_lone_surrogates(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("a string with a lone surrogate is not valid Unicode") from error


JsonObject = Annotated[dict[str, JsonValue], AfterValidator(_refuse_non_json_object)]
"""The one decider of a JSON object on the wire: one that survives a round trip unchanged."""


def loaded_json(text: str | bytes) -> JsonValue:
    """``json.loads`` of a wire document, typed so a reader narrows it with ``isinstance``."""
    document: JsonValue = json.loads(text)
    return document


def _refuse_non_unicode_text(text: str) -> str:
    _refuse_lone_surrogates(text)
    return text


UnicodeText = Annotated[str, AfterValidator(_refuse_non_unicode_text)]
"""The one type of wire text: valid Unicode, so the body or frame it lands in encodes as JSON."""

NonEmptyUnicodeText = Annotated[UnicodeText, Field(min_length=1)]
"""The one owner of non-empty wire text.

It covers a call id, a tool's name, an assistant text and a principal's identifying
claims.
"""


def _refuse_non_agent(text: str) -> str:
    if _AGENT_PATTERN.fullmatch(text) is None:
        raise ValueError(
            f"must be 1 to {MAX_AGENT_LENGTH} characters from a-z, 0-9 and -, "
            "starting with a letter"
        )
    return text


AgentName = Annotated[str, AfterValidator(_refuse_non_agent)]
"""The one decider of an agent's name: the agent a run is started for."""

AgentRole = Literal["admin", "user"]
"""The two roles an app gives a person; the host's own role names map onto these."""


def _refuse_non_agent_roles(roles: list[AgentRole]) -> list[AgentRole]:
    if roles not in (["admin"], ["admin", "user"]):
        raise ValueError("must be exactly [admin] or [admin, user]")
    return roles


def _require_list(roles: JsonValue) -> JsonValue:
    if not isinstance(roles, list):
        raise ValueError("must be a list")
    return roles


AgentRoles = Annotated[
    list[AgentRole], BeforeValidator(_require_list), AfterValidator(_refuse_non_agent_roles)
]
"""The one decider of which roles an agent is offered to: admin alone, or admin and user."""


class StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _refuse_non_file_name(name: str) -> str:
    _refuse_lone_surrogates(name)
    if not 1 <= len(name.encode("utf-8")) <= MAX_RUN_FILE_NAME_BYTES:
        raise ValueError(f"must be 1 to {MAX_RUN_FILE_NAME_BYTES} bytes in UTF-8")
    if "/" in name or "\0" in name or name in (".", ".."):
        raise ValueError("must be one path component: no /, no NUL, not . or ..")
    return name


def _read_canonical_base64(value: object) -> object:
    # Pydantic's lenient Base64Bytes accepts whitespace, the URL-safe alphabet
    # and missing padding; the wire accepts one spelling of each content.
    if not isinstance(value, str):
        return value
    try:
        content = base64.b64decode(value.encode("ascii"), validate=True)
    except ValueError:
        content = None
    if content is None or base64.b64encode(content).decode("ascii") != value:
        raise ValueError("must be canonical standard base64 with padding")
    return content


def written_base64(content: bytes) -> str:
    """The one spelling of ``content`` a JSON form and a wire part write."""
    return base64.b64encode(content).decode("ascii")


Base64Content = Annotated[
    bytes,
    BeforeValidator(_read_canonical_base64),
    PlainSerializer(written_base64, return_type=str, when_used="json"),
]


class RunFile(StrictModel):
    """One file a run returns: its name and its bytes, with no media type."""

    name: Annotated[str, AfterValidator(_refuse_non_file_name)]
    content: Base64Content


TOOL_IMAGE_MEDIA_TYPE: Final = "image/jpeg"
_JPEG_START: Final = b"\xff\xd8\xff"


def _refuse_non_jpeg(content: bytes) -> bytes:
    # the start-of-image marker is what the media type promises the model
    if not content.startswith(_JPEG_START):
        raise ValueError("must be a JPEG: empty, or without its start-of-image marker")
    return content


class ToolImage(StrictModel):
    """One JPEG still picture a tool hands the agent's own model, as its bytes."""

    content: Annotated[Base64Content, AfterValidator(_refuse_non_jpeg)]


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class TurnMessage(StrictModel):
    role: Annotated[TurnRole, Field(strict=False)]
    text: UnicodeText


class Turn(StrictModel):
    """The conversation a run answers: the system prompt and the messages so far.

    It names no provider, model, route, user or MCP endpoint: the backend
    decides those from the caller's identity and the app's configuration.
    """

    system: UnicodeText
    messages: list[TurnMessage] = Field(min_length=1)


TokenCount = Annotated[int, Field(ge=0)]


class TurnUsage(StrictModel):
    """The tokens a turn used, in the provider-neutral words of the turn.

    ``input_tokens`` is everything the model read, cached tokens included; ``cached_input_tokens``
    is the part read from the provider's cache and ``cache_write_tokens`` the part written to it.
    """

    input_tokens: TokenCount
    cached_input_tokens: TokenCount
    cache_write_tokens: TokenCount
    output_tokens: TokenCount

    def __add__(self, other: TurnUsage) -> TurnUsage:
        return TurnUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class Principal(StrictModel):
    """The verified caller exactly as the app bound it: no bound, no normalization."""

    iss: NonEmptyUnicodeText
    sub: NonEmptyUnicodeText


def person_fingerprint(principal: Principal) -> str:
    """One recipe for every caller: a change splits every person's budget."""
    identity = json.dumps([principal.iss, principal.sub], ensure_ascii=False, separators=(",", ":"))
    return "fp-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _refuse_non_gateway_model(text: str) -> str:
    if _GATEWAY_MODEL_NAME.fullmatch(text) is None:
        raise ValueError("must be text/<model> or image/<model>, such as text/opus")
    return text


GatewayModelName = Annotated[str, AfterValidator(_refuse_non_gateway_model)]
"""The one decider of a gateway model: its kind (``text`` or ``image``), a slash, the model part."""


def model_kind_of(name: GatewayModelName) -> ModelKind:
    """The one reading of a model name's kind; the name is already a ``GatewayModelName``."""
    return ModelKind(name.partition("/")[0])
