"""One fresh subscription run of the pinned Codex CLI, as the neutral run entry's adapter."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import ClassVar, Final, NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_runtime.anchored_turn import CliProcessPool, ReapBounds
from agent_runtime.cli_run import CliRunStart, StepEvent
from agent_runtime.cli_settings import CliSettings, TomlValue, merged
from agent_runtime.codex_login import CodexLogin, CodexLoginVerdict
from agent_runtime.constants import CLI_GROUP_CONFIRMATION_SECONDS, CLI_TERMINATION_GRACE_SECONDS
from agent_runtime.errors import CliLoginRefusedError, raise_without_chain
from agent_runtime.events import AssistantTextEvent, FinalEvent
from agent_runtime.spawn import ChildProcess, ChildProcessSpecificationError, closed_environment
from agent_runtime.transcript import flatten_messages
from agent_runtime_client.wire import TurnMessage

CODEX_CLI_MAX_CONCURRENT_PROCESSES: Final = 8

CODEX_CLI_PROCESS_POOL: Final = CliProcessPool(
    limit=CODEX_CLI_MAX_CONCURRENT_PROCESSES,
    name="Codex CLI",
    reap=ReapBounds(
        sigterm_grace_seconds=CLI_TERMINATION_GRACE_SECONDS,
        confirmation_seconds=CLI_GROUP_CONFIRMATION_SECONDS,
    ),
)

_STREAM_BUFFER_LIMIT: Final = 4 * 1024 * 1024
_AUTH_FILE_NAME: Final = "auth.json"
_SYSTEM_PROMPT_FILE_NAME: Final = "system-prompt"
_HOME_VARIABLE: Final = "CODEX_HOME"

_CONFIG_FILE_NAME: Final = "config.toml"
_PICTURE_FOLDER_NAME: Final = "generated_images"

_BASE_ARGUMENTS: Final = (
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--ignore-rules",
    "--ephemeral",
    "--strict-config",
)
_FEATURES_OFF: Final = (
    "shell_tool",
    "unified_exec",
    "browser_use",
    "computer_use",
    "multi_agent",
    "image_generation",
    "plugins",
    "hooks",
    "code_mode_host",
    "code_mode",
    "code_mode_only",
)
_DEFAULT_SETTINGS: Final[CliSettings] = {
    "sandbox_mode": "read-only",
    "approval_policy": "never",
    "mcp_servers": {},
    "web_search": "disabled",
    "features": dict.fromkeys(_FEATURES_OFF, False),
}
_READ_PROMPT_FROM_STDIN: Final = "-"

_TOOL_ITEM_TYPES: Final = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "collab_agent_tool_call",
        "image_generation",
        "image_gen",
    }
)
_SILENT_ITEM_TYPES: Final = frozenset({"reasoning", "todo_list", "error"})
_DROPPED_ITEM_TYPES: Final = _TOOL_ITEM_TYPES | _SILENT_ITEM_TYPES
_SILENT_EVENT_TYPES: Final = frozenset({"thread.started", "turn.started", "error"})
_ITEM_EVENT_TYPES: Final = frozenset({"item.started", "item.updated", "item.completed"})
_AGENT_MESSAGE: Final = "agent_message"
CODEX_CLI_LOGIN_REFUSED_DETAIL: Final = "Codex CLI login was refused."
# codex 0.163.0's `turn.failed` carries the HTTP status only inside its message text (ADR A1b-C0a
# N1); a bare `401` is not enough because the text also holds a request id.
_LOGIN_REFUSED_STATUS: Final = "401 Unauthorized"


class CodexRunFailure(Exception):
    """A run that ended closed; each class has one fixed message and carries no CLI text."""

    message: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.message)


class CodexUnknownStreamPart(CodexRunFailure):
    message = "The Codex run sent an event or item this adapter does not know."


class CodexUnreadableLine(CodexRunFailure):
    message = "The Codex run sent a line that is not a readable event."


class CodexTurnFailed(CodexRunFailure):
    message = "The Codex turn failed."


class CodexAnswerMissing(CodexRunFailure):
    message = "The Codex run ended without a completed answer."


class _Item(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str


class _MessageItem(_Item):
    text: str = ""


class _StreamEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    item: _Item | None = None


class _ItemEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    item: _Item


class _MessageEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    item: _MessageItem


class _TurnError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str = ""


class _TurnFailedEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    error: _TurnError | None = None


def _fail(failure: CodexRunFailure) -> NoReturn:
    raise_without_chain(failure)


class CodexStreamDecoder:
    """Turns `exec --json` lines into events and fails closed on anything it does not know."""

    def __init__(self) -> None:
        self._completed = False
        self._answer = ""

    def events_of(self, line: bytes) -> Sequence[StepEvent]:
        event = self._parsed(line, _StreamEvent)
        if event.type in _ITEM_EVENT_TYPES:
            return self._item_events(line, self._parsed(line, _ItemEvent))
        if event.type == "turn.completed":
            self._completed = True
        elif event.type == "turn.failed":
            self._fail_turn(self._parsed(line, _TurnFailedEvent))
        elif event.type not in _SILENT_EVENT_TYPES:
            _fail(CodexUnknownStreamPart())
        return []

    @staticmethod
    def _fail_turn(event: _TurnFailedEvent) -> NoReturn:
        if event.error is not None and _LOGIN_REFUSED_STATUS in event.error.message:
            raise_without_chain(CliLoginRefusedError(CODEX_CLI_LOGIN_REFUSED_DETAIL))
        _fail(CodexTurnFailed())

    def ending(self, returncode: int) -> FinalEvent:
        if not self._completed or returncode != 0:
            _fail(CodexAnswerMissing())
        return FinalEvent(text=self._answer)

    @staticmethod
    def _parsed[EventModel: BaseModel](line: bytes, model: type[EventModel]) -> EventModel:
        try:
            return model.model_validate_json(line)
        except ValidationError:
            _fail(CodexUnreadableLine())

    def _item_events(self, line: bytes, event: _ItemEvent) -> list[StepEvent]:
        item = event.item
        if item.type == _AGENT_MESSAGE:
            return self._message_events(event.type, self._parsed(line, _MessageEvent).item)
        if item.type not in _DROPPED_ITEM_TYPES:
            _fail(CodexUnknownStreamPart())
        return []

    def _message_events(self, event_type: str, item: _MessageItem) -> list[StepEvent]:
        if event_type != "item.completed":
            return []
        self._answer = item.text.strip()
        return [AssistantTextEvent(text=item.text)]


def _toml_basic_string(value: str) -> str:
    # JSON string escapes are a subset of TOML basic string escapes, except that TOML also
    # forbids the raw DEL character, which JSON leaves as it is.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


_BARE_KEY: Final = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_basic_string(key)


def _toml_float(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)


def _toml_value(path: str, value: TomlValue) -> str:
    # `bool` is checked before its base class `int`.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _toml_float(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_basic_string(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, list):
        items = (_toml_value(f"{path}[{index}]", item) for index, item in enumerate(value))
        return f"[{', '.join(items)}]"
    if isinstance(value, dict):
        return _toml_inline_table(path, value)
    raise TypeError(f"Setting {path} is a {type(value).__name__}, which TOML cannot hold")


def _toml_inline_table(path: str, table: CliSettings) -> str:
    entries = (
        f"{_toml_key(key)} = {_toml_value(f'{path}.{key}' if path else key, value)}"
        for key, value in table.items()
    )
    return f"{{ {', '.join(entries)} }}" if table else "{}"


def _toml_document(settings: CliSettings) -> bytes:
    lines = (
        f"{_toml_key(key)} = {_toml_value(key, value)}\n" for key, value in settings.items()
    )
    return "".join(lines).encode()


@dataclass(frozen=True)
class CodexRun:
    """One fresh subscription run of the pinned CLI.

    Its defaults switch off the features this adapter names and set the read-only sandbox; every
    other feature follows the pinned CLI, and the run's ``settings`` (the operator's levels,
    lowest first) may change any of them. It describes the run and starts nothing; the entry
    makes the directories and the spawn owner starts the child.
    """

    binary: Path
    login: CodexLogin = field(repr=False)
    system: str = field(repr=False)
    history: tuple[TurnMessage, ...] = field(repr=False)
    model: str | None = field(kw_only=True)
    settings: tuple[CliSettings, ...] = field(kw_only=True, repr=False)

    def __post_init__(self) -> None:
        if not self.binary.is_absolute():
            raise ChildProcessSpecificationError("A Codex run needs an absolute binary")
        if not (
            isinstance(self.login, CodexLogin) and self.login.verdict is CodexLoginVerdict.USABLE
        ):
            raise ChildProcessSpecificationError("A Codex run needs a usable login")
        if not self.history:
            raise ChildProcessSpecificationError("A Codex run needs a non-empty history")

    @property
    def pool(self) -> CliProcessPool:
        return CODEX_CLI_PROCESS_POOL

    @property
    def line_limit_bytes(self) -> int:
        return _STREAM_BUFFER_LIMIT

    def run_files(self) -> Mapping[str, bytes]:
        return {
            _AUTH_FILE_NAME: self.login.blanked_document,
            _SYSTEM_PROMPT_FILE_NAME: self.system.encode(),
            _CONFIG_FILE_NAME: _toml_document(merged(_DEFAULT_SETTINGS, *self.settings)),
        }

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        child = ChildProcess(
            binary=self.binary,
            arguments=self._arguments(home),
            environment=closed_environment(home, **{_HOME_VARIABLE: str(home)}),
            working_directory=working_directory,
        )
        return CliRunStart(
            child, self._transcript().encode(), CodexStreamDecoder(), (_PICTURE_FOLDER_NAME,)
        )

    def _transcript(self) -> str:
        return flatten_messages(
            "",
            [{"role": message.role.value, "content": message.text} for message in self.history],
        )

    def _model_option(self) -> tuple[str, ...]:
        # One `--model=<name>` argument, so no name can be read as another option.
        return () if self.model is None else (f"--model={self.model}",)

    def _arguments(self, home: Path) -> tuple[str, ...]:
        system_file = _toml_basic_string(str(home / _SYSTEM_PROMPT_FILE_NAME))
        return (
            *_BASE_ARGUMENTS,
            "-c",
            f"model_instructions_file={system_file}",
            *self._model_option(),
            _READ_PROMPT_FROM_STDIN,
        )
