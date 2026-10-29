"""One fresh, tool-free subscription run of the Grok CLI, as the neutral run entry's adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, NoReturn

from pydantic import BaseModel, ConfigDict

from agent_runtime.anchored_turn import CliProcessPool, ReapBounds
from agent_runtime.cli_run import (
    CliRunFailure,
    CliRunStart,
    StepEvent,
    parsed_line,
    require_runnable,
)
from agent_runtime.constants import CLI_GROUP_CONFIRMATION_SECONDS, CLI_TERMINATION_GRACE_SECONDS
from agent_runtime.errors import CliLoginRefusedError, raise_without_chain
from agent_runtime.events import AssistantTextEvent, FinalEvent
from agent_runtime.grok_login import GrokLogin, GrokLoginVerdict
from agent_runtime.spawn import ChildProcess, closed_environment
from agent_runtime.transcript import flatten_messages
from agent_runtime_client.wire import TurnMessage

GROK_CLI_MAX_CONCURRENT_PROCESSES: Final = 8

GROK_CLI_PROCESS_POOL: Final = CliProcessPool(
    limit=GROK_CLI_MAX_CONCURRENT_PROCESSES,
    name="Grok CLI",
    reap=ReapBounds(
        sigterm_grace_seconds=CLI_TERMINATION_GRACE_SECONDS,
        confirmation_seconds=CLI_GROUP_CONFIRMATION_SECONDS,
    ),
)

_STREAM_BUFFER_LIMIT: Final = 4 * 1024 * 1024
_AUTH_FILE_NAME: Final = "auth.json"
_RULES_FILE_NAME: Final = "AGENTS.md"
_PROMPT_FILE_NAME: Final = "prompt.txt"
_HOME_VARIABLE: Final = "GROK_HOME"

_BASE_ARGUMENTS: Final = (
    "--output-format",
    "streaming-json",
    "--deny",
    "*",
    "--max-turns",
    "1",
    "--no-subagents",
    "--disable-web-search",
)

_TEXT: Final = "text"
_END: Final = "end"
_ERROR: Final = "error"
_TOOL_EVENT_TYPES: Final = frozenset({"tool_call", "tool_call_update"})
_SILENT_EVENT_TYPES: Final = frozenset({"thought", "usage", "available_commands"})
GROK_CLI_LOGIN_REFUSED_DETAIL: Final = "Grok CLI login was refused."
# grok 1.0.60 reports a missing login as an `error` event whose message starts with this (ADR
# A1b-G0a N1).
_LOGIN_REFUSED_TEXT: Final = "Not signed in"


class GrokRunFailure(CliRunFailure):
    """A Grok run that ended closed."""


class GrokUnknownStreamPart(GrokRunFailure):
    message = "The Grok run sent an event this adapter does not know."


class GrokUnreadableLine(GrokRunFailure):
    message = "The Grok run sent a line that is not a readable event."


class GrokToolCalled(GrokRunFailure):
    message = "The Grok run called a tool in a turn that allows none."


class GrokTurnFailed(GrokRunFailure):
    message = "The Grok turn failed."


class GrokAnswerMissing(GrokRunFailure):
    message = "The Grok run ended without a completed answer."


class _StreamEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str


class _TextEvent(_StreamEvent):
    data: str


class _ErrorEvent(_StreamEvent):
    message: str = ""


class GrokStreamDecoder:
    """Turns `streaming-json` lines into events and fails closed on anything it does not know."""

    def __init__(self) -> None:
        self._ended = False
        self._answer = ""

    def events_of(self, line: bytes) -> Sequence[StepEvent]:
        event_type = parsed_line(line, _StreamEvent, GrokUnreadableLine()).type
        if event_type == _TEXT:
            text = parsed_line(line, _TextEvent, GrokUnreadableLine()).data
            self._answer += text
            return [AssistantTextEvent(text=text)]
        if event_type == _END:
            self._ended = True
        elif event_type == _ERROR:
            self._fail_turn(parsed_line(line, _ErrorEvent, GrokUnreadableLine()))
        elif event_type in _TOOL_EVENT_TYPES:
            raise_without_chain(GrokToolCalled())
        elif event_type not in _SILENT_EVENT_TYPES:
            raise_without_chain(GrokUnknownStreamPart())
        return []

    @staticmethod
    def _fail_turn(event: _ErrorEvent) -> NoReturn:
        if event.message.startswith(_LOGIN_REFUSED_TEXT):
            raise_without_chain(CliLoginRefusedError(GROK_CLI_LOGIN_REFUSED_DETAIL))
        raise_without_chain(GrokTurnFailed())

    def ending(self, returncode: int) -> FinalEvent:
        if not self._ended or returncode != 0:
            raise_without_chain(GrokAnswerMissing())
        return FinalEvent(text=self._answer.strip())


@dataclass(frozen=True)
class GrokRun:
    """One fresh, tool-free subscription run of the Grok CLI.

    It describes the run and starts nothing; the entry makes the directories and the spawn owner
    starts the child. The system text and the prompt are files in the run's home, never argv.
    """

    binary: Path
    login: GrokLogin = field(repr=False)
    system: str = field(repr=False)
    history: tuple[TurnMessage, ...] = field(repr=False)
    model: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        require_runnable("Grok", self, GrokLogin, GrokLoginVerdict.USABLE)

    @property
    def pool(self) -> CliProcessPool:
        return GROK_CLI_PROCESS_POOL

    @property
    def line_limit_bytes(self) -> int:
        return _STREAM_BUFFER_LIMIT

    def run_files(self) -> Mapping[str, bytes]:
        return {
            _AUTH_FILE_NAME: self.login.blanked_document,
            _RULES_FILE_NAME: self.system.encode(),
            _PROMPT_FILE_NAME: self._prompt().encode(),
        }

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        child = ChildProcess(
            binary=self.binary,
            arguments=self._arguments(home),
            environment=closed_environment(home, **{_HOME_VARIABLE: str(home)}),
            working_directory=working_directory,
        )
        return CliRunStart(child, b"", GrokStreamDecoder())

    def _prompt(self) -> str:
        return flatten_messages(
            "",
            [{"role": message.role.value, "content": message.text} for message in self.history],
        )

    def _arguments(self, home: Path) -> tuple[str, ...]:
        # One `--model=<name>` argument, so no name can be read as another option.
        model_option = () if self.model is None else (f"--model={self.model}",)
        return ("--prompt-file", str(home / _PROMPT_FILE_NAME), *_BASE_ARGUMENTS, *model_option)
