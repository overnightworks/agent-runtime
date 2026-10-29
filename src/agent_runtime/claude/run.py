"""One fresh subscription run of the pinned Claude CLI, as the neutral run entry's adapter."""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

from pydantic import SecretStr

from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.claude.provider import (
    _CLI_MCP_CONNECTED_STATUS,
    _STREAM_BUFFER_LIMIT,
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
from agent_runtime.cli_settings import CliSettings, TomlValue, merged
from agent_runtime.constants import (
    CLAUDE_NO_SESSION_PERSISTENCE_FLAG,
    CLAUDE_OAUTH_TOKEN_VARIABLE,
    CLAUDE_RUN_SWITCHES,
)
from agent_runtime.spawn import ChildProcess, ChildProcessSpecificationError, closed_environment
from agent_runtime.transcript import flatten_messages
from agent_runtime_client.wire import TurnMessage

log = logging.getLogger(__name__)

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
_USER_SETTINGS_FILE_NAME: Final = "settings.json"
_OPERATOR_SETTINGS_FILE_NAME: Final = "cli-settings.json"
_CONFIG_DIRECTORY_VARIABLE: Final = "CLAUDE_CONFIG_DIR"
_LEVEL_KEYS: Final = frozenset({"tools", "settings"})
# The CLI splits `--tools` and `--allowedTools` on commas and spaces and reads a leading dash
# as an option, so such a name cannot be passed faithfully.
_TOOL_NAME_SEPARATOR: Final = re.compile(r"[,\s]")
# The sandbox is on, fails closed when it cannot start, and lets no command leave it. It is
# Claude's user layer in the run home, below the operator's `--settings` file: an invalid
# value in that file makes the CLI drop the whole file, and this default stays.
_SANDBOX_DEFAULT: Final[CliSettings] = {
    "sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}
}


def check_tool_name(name: str) -> None:
    """Refuse a tool name the CLI's option parsing cannot take as one name, without echoing it."""
    if not name or name.startswith("-") or _TOOL_NAME_SEPARATOR.search(name):
        raise ValueError(
            "a tool name must not be empty, hold a comma or whitespace, or start with -"
        )


def _json_value(path: str, value: TomlValue) -> object:
    # `bool` is checked through `int`, its base class.
    if isinstance(value, int | str):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(f"{path}[{index}]", item) for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {key: _json_value(f"{path}.{key}", item) for key, item in value.items()}
    raise TypeError(f"Setting {path} is a {type(value).__name__}, which JSON cannot hold")


def settings_document(settings: CliSettings) -> bytes:
    """The settings as a Claude settings file; a value JSON cannot hold raises `TypeError`."""
    return json.dumps(_json_value("settings", settings), allow_nan=False).encode()


def _checked_level(level: object) -> None:
    if not isinstance(level, dict) or level.keys() - _LEVEL_KEYS:
        raise ChildProcessSpecificationError(
            "A Claude run's settings level holds only tools and settings"
        )
    tools = level.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(name, str) for name in tools):
        raise ChildProcessSpecificationError("A Claude run's tools must be a list of strings")
    if not isinstance(level.get("settings", {}), dict):
        raise ChildProcessSpecificationError("A Claude run's settings must be a table")
    for name in tools:
        try:
            check_tool_name(name)
        except ValueError as refusal:
            raise ChildProcessSpecificationError(f"A Claude run's tools: {refusal}") from None


def _relay_state_of(servers: tuple[tuple[str, str], ...]) -> RelayState | None:
    statuses = [status for name, status in servers if name == RELAY_SERVER_NAME]
    if len(statuses) > 1:
        return None
    if not statuses:
        return RelayState.ABSENT
    if statuses[0] == _CLI_MCP_CONNECTED_STATUS:
        return RelayState.CONNECTED
    return RelayState.NOT_CONNECTED


def _run_surface_of(
    payload: dict, *, relay_attached: bool, listed_tools: frozenset[str]
) -> RunSurface:
    tools = _announced_tools(payload)
    servers = _announced_servers(payload)
    api_key_source = payload.get("apiKeySource")
    relay = None if servers is None else _relay_state_of(servers)
    if tools is None or servers is None or relay is None or not isinstance(api_key_source, str):
        refuse_announcement(SurfaceVerdict.UNREADABLE)
    relay_prefix = _mcp_tool_prefix(RELAY_SERVER_NAME)
    missing = listed_tools.difference(tools)
    if missing:
        log.warning("A listed tool was not announced: %s", ", ".join(sorted(missing)))
    return RunSurface(
        foreign_credential=api_key_source != _CLI_NO_API_KEY_SOURCE,
        unlisted_tool=any(
            tool not in listed_tools and not (relay_attached and tool.startswith(relay_prefix))
            for tool in tools
        ),
        listed_tool_missing=bool(missing),
        server_beside_relay=any(name != RELAY_SERVER_NAME for name, _ in servers),
        relay=relay,
    )


class ClaudeStreamDecoder(_ClaudeLineDecoder):
    """The line decoder that also judges the run's announcement.

    The first object must be ``system/init``, and every init line is judged. It announces exactly
    the run's ``listed_tools`` and, with a relay attached, the relay's tools; a name the log
    carries for a missing tool comes from ``listed_tools``, never from the announcement.
    """

    def __init__(self, *, relay_attached: bool, listed_tools: frozenset[str]) -> None:
        super().__init__()
        self._relay_attached = relay_attached
        self._listed_tools = listed_tools
        self._announced = False

    def _events_of_payload(self, payload: dict) -> list[StepEvent]:
        if _is_init_event(payload):
            examine_announcement(
                _run_surface_of(
                    payload,
                    relay_attached=self._relay_attached,
                    listed_tools=self._listed_tools,
                ),
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

    Its ``settings`` are the operator's levels, lowest first, each ``{tools, settings}`` with a
    field a level does not set absent. They are merged once; the effective ``tools`` are the
    built-in tools the run is given, and the effective ``settings`` are Claude's own settings
    file, passed as ``--settings``. With tools, the run also gets the sandbox default as the
    user layer of a ``CLAUDE_CONFIG_DIR`` that is the run home.
    """

    binary: Path
    token: SecretStr
    system: str = field(repr=False)
    max_tool_calls: int
    history: tuple[TurnMessage, ...] = field(repr=False)
    relay: RelayAccess | None = field(kw_only=True)
    tool_timeout_seconds: float = field(kw_only=True)
    model: str | None = field(kw_only=True)
    settings: tuple[CliSettings, ...] = field(kw_only=True, repr=False)

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
        for level in self.settings:
            _checked_level(level)

    def _listed_tools(self) -> tuple[str, ...]:
        return tuple(cast("list[str]", merged(*self.settings).get("tools", [])))

    def _operator_settings(self) -> CliSettings:
        return cast("CliSettings", merged(*self.settings).get("settings", {}))

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
        if self._listed_tools():
            files[_USER_SETTINGS_FILE_NAME] = settings_document(_SANDBOX_DEFAULT)
        if operator_settings := self._operator_settings():
            files[_OPERATOR_SETTINGS_FILE_NAME] = settings_document(operator_settings)
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

    def _config_directory_environment(self, home: Path) -> dict[str, str]:
        return {_CONFIG_DIRECTORY_VARIABLE: str(home)} if self._listed_tools() else {}

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        child = ChildProcess(
            binary=self.binary,
            arguments=self._arguments(home),
            environment=closed_environment(
                home,
                **{CLAUDE_OAUTH_TOKEN_VARIABLE: self.token.get_secret_value()},
                **CLAUDE_RUN_SWITCHES,
                **self._tool_timeout_environment(),
                **self._config_directory_environment(home),
            ),
            working_directory=working_directory,
        )
        decoder = ClaudeStreamDecoder(
            relay_attached=self.relay is not None, listed_tools=frozenset(self._listed_tools())
        )
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

    def _isolation_flags(self) -> tuple[str, ...]:
        tools = self._listed_tools()
        return (
            "--tools",
            ",".join(tools),
            "--setting-sources",
            "user" if tools else "",
            "--strict-mcp-config",
            "--disable-slash-commands",
        )

    def _settings_option(self, home: Path) -> list[str]:
        if not self._operator_settings():
            return []
        return ["--settings", str(home / _OPERATOR_SETTINGS_FILE_NAME)]

    def _relay_option(self, home: Path) -> list[str]:
        if self.relay is None:
            return []
        return ["--mcp-config", str(home / _RELAY_CONFIG_FILE_NAME)]

    def _allowed_tools_option(self) -> tuple[str, ...]:
        relay_tools = () if self.relay is None else (f"{_mcp_tool_prefix(RELAY_SERVER_NAME)}*",)
        allowed = (*relay_tools, *self._listed_tools())
        return ("--allowedTools", ",".join(allowed)) if allowed else ()

    def _arguments(self, home: Path) -> tuple[str, ...]:
        return (
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            *self._isolation_flags(),
            CLAUDE_NO_SESSION_PERSISTENCE_FLAG,
            "--max-turns",
            str(self.max_tool_calls + TURNS_BEYOND_TOOL_CALLS),
            "--system-prompt-file",
            str(home / _SYSTEM_PROMPT_FILE_NAME),
            *self._model_option(),
            *self._settings_option(home),
            *self._relay_option(home),
            *self._allowed_tools_option(),
        )
