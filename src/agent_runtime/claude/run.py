"""One fresh subscription run of the pinned Claude CLI, as the neutral run entry's adapter."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.claude.provider import (
    _CLI_MCP_CONNECTED_STATUS,
    _STREAM_BUFFER_LIMIT,
    _TOOL_ISOLATION_FLAGS,
    CLAUDE_CLI_PROCESS_POOL,
    _announced_servers,
    _announced_tools,
    _ClaudeLineDecoder,
    _is_init_event,
    _mcp_tool_prefix,
)
from agent_runtime.cli_run import (
    CliRunStart,
    RelayAccess,
    RelayState,
    RunSurface,
    StepEvent,
    SurfaceVerdict,
    examine_announcement,
    refuse_announcement,
    relay_authorization,
)
from agent_runtime.constants import (
    CLAUDE_NO_SESSION_PERSISTENCE_FLAG,
    CLAUDE_OAUTH_TOKEN_VARIABLE,
    CLAUDE_RUN_SWITCHES,
)
from agent_runtime.spawn import ChildProcess, ChildProcessSpecificationError, closed_environment
from agent_runtime.transcript import flatten_messages
from agent_runtime_client.wire import TurnMessage

RELAY_SERVER_NAME: Final = "relay"
_TOOL_TIMEOUT_VARIABLE: Final = "MCP_TOOL_TIMEOUT"
_MILLISECONDS_PER_SECOND: Final = 1000
# The CLI counts one turn per tool-using response and checks its limit after
# that response's calls ran, so one turn beyond the service's call limit stays
# behind it (ADR 0001, A1b-0b2 and A1b-4a).
TURNS_BEYOND_TOOL_CALLS: Final = 1
_CLI_NO_API_KEY_SOURCE: Final = "none"
_SYSTEM_PROMPT_FILE_NAME: Final = "system-prompt"
_RELAY_CONFIG_FILE_NAME: Final = "relay-mcp.json"


def _relay_state_of(servers: tuple[tuple[str, str], ...]) -> RelayState | None:
    statuses = [status for name, status in servers if name == RELAY_SERVER_NAME]
    if len(statuses) > 1:
        return None
    if not statuses:
        return RelayState.ABSENT
    if statuses[0] == _CLI_MCP_CONNECTED_STATUS:
        return RelayState.CONNECTED
    return RelayState.NOT_CONNECTED


def _run_surface_of(payload: dict, *, relay_attached: bool) -> RunSurface:
    tools = _announced_tools(payload)
    servers = _announced_servers(payload)
    api_key_source = payload.get("apiKeySource")
    relay = None if servers is None else _relay_state_of(servers)
    if tools is None or servers is None or relay is None or not isinstance(api_key_source, str):
        refuse_announcement(SurfaceVerdict.UNREADABLE)
    relay_prefix = _mcp_tool_prefix(RELAY_SERVER_NAME)
    return RunSurface(
        foreign_credential=api_key_source != _CLI_NO_API_KEY_SOURCE,
        tool_beside_relay=any(
            not (relay_attached and tool.startswith(relay_prefix)) for tool in tools
        ),
        server_beside_relay=any(name != RELAY_SERVER_NAME for name, _ in servers),
        relay=relay,
    )


class ClaudeStreamDecoder(_ClaudeLineDecoder):
    """The line decoder that also judges the run's announcement.

    The first object must be ``system/init``, and every init line is judged.
    """

    def __init__(self, *, relay_attached: bool) -> None:
        super().__init__()
        self._relay_attached = relay_attached
        self._announced = False

    def _events_of_payload(self, payload: dict) -> list[StepEvent]:
        if _is_init_event(payload):
            examine_announcement(
                _run_surface_of(payload, relay_attached=self._relay_attached),
                relay_attached=self._relay_attached,
            )
            self._announced = True
            return []
        if not self._announced:
            refuse_announcement(SurfaceVerdict.NOT_ANNOUNCED)
        return super()._events_of_payload(payload)


@dataclass(frozen=True)
class ClaudeRun:
    """One subscription run of the pinned CLI, as the neutral run entry's adapter.

    It describes the run and starts nothing; the entry makes the directories and
    the spawn owner starts the child. Its `relay` is attached as given, on the
    trust `RelayAccess` states.
    """

    binary: Path
    token: SecretStr
    system: str = field(repr=False)
    max_tool_calls: int
    history: tuple[TurnMessage, ...] = field(repr=False)
    relay: RelayAccess | None = field(kw_only=True)
    tool_timeout_seconds: float = field(kw_only=True)
    model: str | None = field(kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.token, SecretStr):
            raise ChildProcessSpecificationError("A Claude run needs its token as a SecretStr")
        if not self.binary.is_absolute():
            raise ChildProcessSpecificationError("A Claude run needs an absolute binary")
        if self.max_tool_calls < 1:
            raise ChildProcessSpecificationError("A Claude run needs max_tool_calls of at least 1")
        if not self.history:
            raise ChildProcessSpecificationError("A Claude run needs a non-empty history")
        if not (math.isfinite(self.tool_timeout_seconds) and self.tool_timeout_seconds > 0):
            raise ChildProcessSpecificationError(
                "A Claude run needs a finite, positive tool_timeout_seconds"
            )

    @property
    def pool(self) -> CliProcessPool:
        return CLAUDE_CLI_PROCESS_POOL

    @property
    def line_limit_bytes(self) -> int:
        return _STREAM_BUFFER_LIMIT

    def run_files(self) -> dict[str, bytes]:
        files = {_SYSTEM_PROMPT_FILE_NAME: self.system.encode()}
        if self.relay is not None:
            files[_RELAY_CONFIG_FILE_NAME] = self._relay_config(self.relay)
        return files

    @staticmethod
    def _relay_config(relay: RelayAccess) -> bytes:
        server = {
            "type": "http",
            "url": relay.url,
            "headers": {"Authorization": relay_authorization(relay.secret)},
        }
        return json.dumps({"mcpServers": {RELAY_SERVER_NAME: server}}).encode()

    def _tool_timeout_environment(self) -> dict[str, str]:
        if self.relay is None:
            return {}
        milliseconds = math.ceil(self.tool_timeout_seconds * _MILLISECONDS_PER_SECOND)
        return {_TOOL_TIMEOUT_VARIABLE: str(milliseconds)}

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        child = ChildProcess(
            binary=self.binary,
            arguments=self._arguments(home),
            environment=closed_environment(
                home,
                **{CLAUDE_OAUTH_TOKEN_VARIABLE: self.token.get_secret_value()},
                **CLAUDE_RUN_SWITCHES,
                **self._tool_timeout_environment(),
            ),
            working_directory=working_directory,
        )
        decoder = ClaudeStreamDecoder(relay_attached=self.relay is not None)
        return CliRunStart(child, self.stdin_message(), decoder)

    def stdin_message(self) -> bytes:
        transcript = flatten_messages(
            "",
            [{"role": message.role.value, "content": message.text} for message in self.history],
        )
        line = {"type": "user", "message": {"role": "user", "content": transcript}}
        return (json.dumps(line) + "\n").encode()

    def _model_option(self) -> tuple[str, ...]:
        # One `--model=<name>` argument, so no name can be read as another option.
        return () if self.model is None else (f"--model={self.model}",)

    def _arguments(self, home: Path) -> tuple[str, ...]:
        arguments = (
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            *_TOOL_ISOLATION_FLAGS,
            CLAUDE_NO_SESSION_PERSISTENCE_FLAG,
            "--max-turns",
            str(self.max_tool_calls + TURNS_BEYOND_TOOL_CALLS),
            "--system-prompt-file",
            str(home / _SYSTEM_PROMPT_FILE_NAME),
            *self._model_option(),
        )
        if self.relay is None:
            return arguments
        return (
            *arguments,
            "--mcp-config",
            str(home / _RELAY_CONFIG_FILE_NAME),
            "--allowedTools",
            f"{_mcp_tool_prefix(RELAY_SERVER_NAME)}*",
        )
