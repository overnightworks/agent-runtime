"""One fresh subscription run of the pinned Codex CLI, as the neutral run entry's adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Final, NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_runtime.anchored_turn import CliProcessPool, ReapBounds
from agent_runtime.cli_run import CliRunStart, StepEvent
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

_BASE_ARGUMENTS: Final = (
    "exec",
    "--json",
    "--sandbox",
    "read-only",
    "--skip-git-repo-check",
    "--ignore-user-config",
    "--ignore-rules",
    "--ephemeral",
    "--strict-config",
)
_TOOL_FEATURES_OFF: Final = (
    "shell_tool",
    "unified_exec",
    "browser_use",
    "computer_use",
    "multi_agent",
    "image_generation",
    "plugins",
    "hooks",
)
_CODE_MODE_FEATURES_OFF: Final = ("code_mode_host", "code_mode", "code_mode_only")
_TOOL_ISOLATION_SWITCHES: Final = (
    'approval_policy="never"',
    "mcp_servers={}",
    *(f"features.{name}=false" for name in _TOOL_FEATURES_OFF),
    'web_search="disabled"',
    *(f"features.{name}=false" for name in _CODE_MODE_FEATURES_OFF),
)
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


@dataclass(frozen=True)
class CodexRun:
    """One fresh, tool-free subscription run of the pinned CLI.

    It describes the run and starts nothing; the entry makes the directories and
    the spawn owner starts the child.
    """

    binary: Path
    login: CodexLogin = field(repr=False)
    system: str = field(repr=False)
    history: tuple[TurnMessage, ...] = field(repr=False)
    model: str | None = field(kw_only=True)

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
        }

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        child = ChildProcess(
            binary=self.binary,
            arguments=self._arguments(home),
            environment=closed_environment(home, **{_HOME_VARIABLE: str(home)}),
            working_directory=working_directory,
        )
        return CliRunStart(child, self._transcript().encode(), CodexStreamDecoder())

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
        switches = (*_TOOL_ISOLATION_SWITCHES, f"model_instructions_file={system_file}")
        return (
            *_BASE_ARGUMENTS,
            *(part for switch in switches for part in ("-c", switch)),
            *self._model_option(),
            _READ_PROMPT_FROM_STDIN,
        )
