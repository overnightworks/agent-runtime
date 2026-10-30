"""One fresh subscription run of the Grok CLI, as the neutral run entry's adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, NoReturn

from pydantic import BaseModel, ConfigDict, JsonValue

from agent_runtime.anchored_turn import CliProcessPool, ReapBounds
from agent_runtime.cli_run import (
    RELAY_SERVER_NAME,
    RELAY_TOKEN_VARIABLE,
    CliRunFailure,
    CliRunStart,
    RelayAccess,
    StepEvent,
    parsed_line,
    require_runnable,
)
from agent_runtime.cli_settings import CliSettings, merged, toml_document, toml_key
from agent_runtime.constants import CLI_GROUP_CONFIRMATION_SECONDS, CLI_TERMINATION_GRACE_SECONDS
from agent_runtime.errors import CliLoginRefusedError, raise_without_chain
from agent_runtime.events import AssistantTextEvent, FinalEvent
from agent_runtime.grok_login import GrokLogin, GrokLoginVerdict
from agent_runtime.spawn import ChildProcess, ChildProcessSpecificationError, closed_environment
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
_CONFIG_FILE_NAME: Final = "config.toml"
_HOME_VARIABLE: Final = "GROK_HOME"

_OUTPUT_ARGUMENTS: Final = ("--output-format", "streaming-json")
_TOOL_FREE_ARGUMENTS: Final = ("--deny", "*", "--max-turns", "1")
_CLOSING_ARGUMENTS: Final = ("--no-subagents", "--disable-web-search")
_RELAY_TOOL_PREFIX: Final = f"{RELAY_SERVER_NAME}__"
# A deny rule wins over every allow rule, so a relay run denies the families one by one instead of
# `*` (ADR A1b-X0c-0); which of them the CLI takes is not yet measured (ADR A1b-5r).
_RELAY_DENIED_FAMILIES: Final = (
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    "WebFetch",
    "WebSearch",
)
_RELAY_PERMISSION_ARGUMENTS: Final = (
    "--tools",
    "",
    "--permission-mode",
    "dontAsk",
    "--allow",
    f"MCPTool({_RELAY_TOOL_PREFIX}*)",
    *(argument for family in _RELAY_DENIED_FAMILIES for argument in ("--deny", family)),
)
# The CLI's turn limit stays one turn behind the service's call limit (ADR A1b-5r), and the CLI
# parses `--max-turns` only in 1..4294967295 (ADR A1b-G0a L1).
_TURNS_BEYOND_TOOL_CALLS: Final = 1
_LARGEST_TURN_LIMIT: Final = 4_294_967_295

_TEXT: Final = "text"
_END: Final = "end"
_ERROR: Final = "error"
_TOOL_CALL: Final = "tool_call"
_TOOL_CALL_UPDATE: Final = "tool_call_update"
_TURN_LIMIT_REACHED: Final = "max_turns_reached"
_USE_TOOL: Final = "use_tool"
_SEARCH_TOOL: Final = "search_tool"
_RELAY_CALL_GOING_STATUSES: Final = frozenset({"pending", "in_progress", "completed"})
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
    message = "The Grok run called a tool outside the run's relay."


class GrokRelayCallRefused(GrokRunFailure):
    message = "The Grok run's call to its relay did not complete."


class GrokTurnLimitReached(GrokRunFailure):
    message = "The Grok run reached its turn limit."


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


class _ToolCallEvent(_StreamEvent):
    toolCallId: str
    toolName: str
    rawInput: JsonValue = None


class _ToolCallUpdateEvent(_StreamEvent):
    toolCallId: JsonValue = None
    status: JsonValue = None


def _is_relay_tool(name: JsonValue) -> bool:
    return (
        isinstance(name, str)
        and name.startswith(_RELAY_TOOL_PREFIX)
        and len(name) > len(_RELAY_TOOL_PREFIX)
    )


def _is_relay_call(call: _ToolCallEvent) -> bool:
    if call.toolName == _SEARCH_TOOL:
        return True
    if call.toolName == _USE_TOOL:
        return isinstance(call.rawInput, dict) and _is_relay_tool(call.rawInput.get("tool_name"))
    return _is_relay_tool(call.toolName)


def _is_going(status: JsonValue) -> bool:
    return status is None or (isinstance(status, str) and status in _RELAY_CALL_GOING_STATUSES)


class GrokStreamDecoder:
    """Turns `streaming-json` lines into events and fails closed on anything it does not know.

    With ``relay_attached`` it drops the run's relay calls and their updates while they run or
    complete, because the relay's own events are the run's tool frames; a relay call with any
    other status, and every other tool event, ends the run.
    """

    def __init__(self, *, relay_attached: bool) -> None:
        self._relay_attached = relay_attached
        self._relay_calls: set[str] = set()
        self._ended = False
        self._answer = ""

    def events_of(self, line: bytes) -> Sequence[StepEvent]:
        event_type = parsed_line(line, _StreamEvent, GrokUnreadableLine).type
        if event_type == _TEXT:
            text = parsed_line(line, _TextEvent, GrokUnreadableLine).data
            self._answer += text
            return [AssistantTextEvent(text=text)]
        if event_type == _END:
            self._ended = True
        elif event_type == _ERROR:
            self._fail_turn(parsed_line(line, _ErrorEvent, GrokUnreadableLine))
        elif event_type == _TOOL_CALL:
            self._admit_call(line)
        elif event_type == _TOOL_CALL_UPDATE:
            self._admit_update(line)
        elif event_type == _TURN_LIMIT_REACHED:
            raise_without_chain(GrokTurnLimitReached())
        elif event_type not in _SILENT_EVENT_TYPES:
            raise_without_chain(GrokUnknownStreamPart())
        return []

    def _admit_call(self, line: bytes) -> None:
        if not self._relay_attached:
            raise_without_chain(GrokToolCalled())
        call = parsed_line(line, _ToolCallEvent, GrokUnreadableLine)
        if not _is_relay_call(call):
            raise_without_chain(GrokToolCalled())
        self._relay_calls.add(call.toolCallId)

    def _admit_update(self, line: bytes) -> None:
        if not self._relay_attached:
            raise_without_chain(GrokToolCalled())
        update = parsed_line(line, _ToolCallUpdateEvent, GrokUnreadableLine)
        if not (isinstance(update.toolCallId, str) and update.toolCallId in self._relay_calls):
            raise_without_chain(GrokToolCalled())
        if not _is_going(update.status):
            raise_without_chain(GrokRelayCallRefused())

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
    """One fresh subscription run of the Grok CLI.

    It describes the run and starts nothing; the entry makes the directories and the spawn owner
    starts the child. The system text and the prompt are files in the run's home, never argv. The
    run's ``settings`` (the operator's levels, lowest first) are merged and written as the home's
    ``config.toml``, the CLI's user layer, even when empty; the permission switches stay on argv,
    and no setting reaches argv or the environment. Without a ``relay`` the run allows no tool.
    With one, ``config.toml`` gets the ``mcp_servers.relay`` entry over the levels, which may name
    no other server, argv opens only the relay's tools, ``--max-turns`` stays one turn behind
    ``max_tool_calls``, and the relay's secret reaches the child only in its environment, under the
    variable that entry names.
    """

    binary: Path
    login: GrokLogin = field(repr=False)
    system: str = field(repr=False)
    history: tuple[TurnMessage, ...] = field(repr=False)
    model: str | None = field(default=None, kw_only=True)
    settings: tuple[CliSettings, ...] = field(kw_only=True, repr=False)
    relay: RelayAccess | None = field(default=None, kw_only=True)
    tool_timeout_seconds: float | None = field(default=None, kw_only=True)
    max_tool_calls: int | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        require_runnable("Grok", self, GrokLogin, GrokLoginVerdict.USABLE)
        attachment = (self.relay, self.tool_timeout_seconds, self.max_tool_calls)
        if len({part is None for part in attachment}) > 1:
            raise ChildProcessSpecificationError(
                "A Grok run takes a relay, its tool_timeout_seconds and its max_tool_calls"
                " together or none"
            )
        if self.relay is not None:
            self._require_relay_limits()
            self._require_no_server_beside_the_relay()

    def _require_relay_limits(self) -> None:
        timeout = self.tool_timeout_seconds
        if not (timeout is not None and math.isfinite(timeout) and timeout > 0):
            raise ChildProcessSpecificationError(
                "A Grok run needs a finite, positive tool_timeout_seconds"
            )
        calls = self.max_tool_calls
        if not (
            isinstance(calls, int)
            and not isinstance(calls, bool)
            and 1 <= calls <= _LARGEST_TURN_LIMIT - _TURNS_BEYOND_TOOL_CALLS
        ):
            raise ChildProcessSpecificationError(
                "A Grok run needs max_tool_calls as an integer within the CLI's turn limit"
            )

    def _require_no_server_beside_the_relay(self) -> None:
        for level in self.settings:
            servers = level.get("mcp_servers")
            if not isinstance(servers, dict):
                continue
            other = next((name for name in servers if name != RELAY_SERVER_NAME), None)
            if other is not None:
                raise ChildProcessSpecificationError(
                    "A Grok run with a relay takes no other MCP server,"
                    f" not mcp_servers.{toml_key(other)}"
                )

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
            _CONFIG_FILE_NAME: toml_document(self._settings()),
        }

    def _settings(self) -> CliSettings:
        settings = merged(*self.settings)
        if self.relay is None or self.tool_timeout_seconds is None:
            return settings
        settings["mcp_servers"] = {
            RELAY_SERVER_NAME: {
                "url": self.relay.url,
                "bearer_token_env_var": RELAY_TOKEN_VARIABLE,
                "tool_timeout_sec": math.ceil(self.tool_timeout_seconds),
            }
        }
        return settings

    def _relay_environment(self) -> dict[str, str]:
        return {} if self.relay is None else {RELAY_TOKEN_VARIABLE: self.relay.secret}

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        child = ChildProcess(
            binary=self.binary,
            arguments=self._arguments(home),
            environment=closed_environment(
                home, **{_HOME_VARIABLE: str(home)}, **self._relay_environment()
            ),
            working_directory=working_directory,
        )
        return CliRunStart(child, b"", GrokStreamDecoder(relay_attached=self.relay is not None))

    def _prompt(self) -> str:
        return flatten_messages(
            "",
            [{"role": message.role.value, "content": message.text} for message in self.history],
        )

    def _arguments(self, home: Path) -> tuple[str, ...]:
        # One `--model=<name>` argument, so no name can be read as another option.
        model_option = () if self.model is None else (f"--model={self.model}",)
        return (
            "--prompt-file",
            str(home / _PROMPT_FILE_NAME),
            *_OUTPUT_ARGUMENTS,
            *self._permission_arguments(),
            *_CLOSING_ARGUMENTS,
            *model_option,
        )

    def _permission_arguments(self) -> tuple[str, ...]:
        if self.max_tool_calls is None:
            return _TOOL_FREE_ARGUMENTS
        turn_limit = self.max_tool_calls + _TURNS_BEYOND_TOOL_CALLS
        return (*_RELAY_PERMISSION_ARGUMENTS, "--max-turns", str(turn_limit))
