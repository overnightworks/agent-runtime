"""The words an app uses for a turn: its conversation, files and failure codes.

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
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
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
    """Why a run ended as ``failed``."""

    INTERRUPTED = "interrupted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"
    LIMIT_EXCEEDED = "limit_exceeded"
    IDLE_TIMEOUT = "idle_timeout"
    INTERNAL_ERROR = "internal_error"


class RejectionCode(StrEnum):
    """Why the service refused a request: to start a run, or to read one."""

    NOT_ENABLED = "not_enabled"
    BUSY = "busy"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    INVALID_REQUEST = "invalid_request"
    TOO_LARGE = "too_large"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    CAPABILITY_TOO_LARGE = "capability_too_large"
    MODEL_NOT_OFFERED = "model_not_offered"


def refuse_non_json_constant(constant: str) -> object:
    """The one decider of NaN and Infinity in text being parsed: JSON has no word for them.

    Passed as ``parse_constant`` by every reader of untrusted JSON text: the SSE
    frame decoder, the text tool-call protocol and the service's request body.
    """
    raise ValueError(f"{constant} is not a JSON value")


def _refuse_non_json_object(value: dict[Any, Any]) -> dict[str, Any]:
    # The depth limit means depth never stops a built input from encoding:
    # pydantic's serializer refuses a value nested about 255 levels deep.
    # Only the containers on the current path count as open, so a value shared
    # by two branches is accepted, as ``json.dumps`` accepts it. The path holds
    # each open container's remaining children, so the walk's own bookkeeping
    # never mixes with the caller's values.
    open_containers: set[int] = set()
    path = [_enter_container(value, open_containers)]
    while path:
        container_id, children = path[-1]
        for child in children:
            if isinstance(child, dict | list):
                path.append(_enter_container(child, open_containers))
                break
            _refuse_non_json_scalar(child)
        else:
            path.pop()
            open_containers.remove(container_id)
    return value


def _enter_container(
    container: dict[Any, Any] | list[Any], open_containers: set[int]
) -> tuple[int, Iterator[object]]:
    if id(container) in open_containers:
        raise ValueError("a value that contains itself is not JSON")
    if len(open_containers) == MAX_TOOL_INPUT_DEPTH:
        raise ValueError(f"tool input nests deeper than {MAX_TOOL_INPUT_DEPTH} objects and arrays")
    open_containers.add(id(container))
    if isinstance(container, dict):
        for key in container:
            _refuse_non_json_key(key)
        return id(container), iter(container.values())
    return id(container), iter(container)


def _refuse_non_json_key(key: object) -> None:
    if not isinstance(key, str):
        raise ValueError(f"a {type(key).__name__} key is not a JSON object key")
    _refuse_lone_surrogates(key)


def _refuse_non_json_scalar(value: object) -> None:
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value} is not a JSON number")
    elif isinstance(value, str):
        _refuse_lone_surrogates(value)
    else:
        raise ValueError(f"a {type(value).__name__} is not a JSON value")


def _refuse_lone_surrogates(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("a string with a lone surrogate is not valid Unicode") from error


JsonObject = Annotated[dict[str, Any], AfterValidator(_refuse_non_json_object)]
"""The one decider of a JSON object on the wire: one that survives a round trip unchanged."""


def _refuse_non_unicode_text(text: str) -> str:
    _refuse_lone_surrogates(text)
    return text


UnicodeText = Annotated[str, AfterValidator(_refuse_non_unicode_text)]
"""The one type of wire text: valid Unicode, so the body or frame it lands in encodes as JSON."""

NonEmptyUnicodeText = Annotated[UnicodeText, Field(min_length=1)]
"""The one owner of non-empty wire text.

It covers a call id, a tool's name, an assistant text and a capability's identifying
claims.
"""

ToolUseId = NonEmptyUnicodeText
"""The one decider of a valid call id, so a call and its result accept the same ids."""


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


def _require_list(roles: object) -> object:
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


def _write_base64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


class RunFile(StrictModel):
    """One file a run returns: its name and its bytes, with no media type."""

    name: Annotated[str, AfterValidator(_refuse_non_file_name)]
    content: Annotated[
        bytes,
        BeforeValidator(_read_canonical_base64),
        PlainSerializer(_write_base64, return_type=str, when_used="json"),
    ]


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class TurnMessage(StrictModel):
    role: Annotated[TurnRole, Field(strict=False)]
    text: UnicodeText


class Turn(StrictModel):
    """The conversation a run answers: the system prompt and the messages so far.

    It names no provider, model, route, user or MCP endpoint: the service
    decides those from the caller's identity and the app's registration.
    """

    system: UnicodeText
    messages: list[TurnMessage] = Field(min_length=1)


class Principal(StrictModel):
    """The verified caller exactly as the service saw it: no bound, no normalization."""

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
