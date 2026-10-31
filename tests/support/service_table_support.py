"""The service's provider tables: each table's config document, login file and stand-in program.

One ``ProviderTable`` describes a table the service serves; the service's startup, run, login and
entrance test modules run their twin rows over ``TABLES``. Test values per table: its login, its
listed model and its pinned version (Codex: ``gpt-5.5`` and 0.163.0; Grok: ``grok-4.6`` and
1.0.60).
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from pydantic import ValidationError
from tests.support.anchored_support import ChildCleanup
from tests.support.cli_tools_support import (
    ClaudeCall,
    CodexChildCapture,
    ToolApp,
    a_relay_calling_codex,
    a_relay_calling_grok,
    a_tool_app,
)
from tests.support.codex_support import DEFAULT_SETTINGS as CODEX_DEFAULT_SETTINGS
from tests.support.codex_support import DONE, THREAD, a_fake_codex, a_message, an_item, codex_line
from tests.support.codex_support import PRECONDITIONS_MARKER as CODEX_PRECONDITIONS_MARKER
from tests.support.codex_support import (
    RELAY_RUN_DEFAULT_SETTINGS as CODEX_RELAY_RUN_DEFAULT_SETTINGS,
)
from tests.support.grok_support import END, a_fake_grok, a_text, grok_line
from tests.support.grok_support import PRECONDITIONS_MARKER as GROK_PRECONDITIONS_MARKER
from tests.support.mcp_app_fake import TestAuthority
from tests.support.provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    SYSTEM_TEXT,
    a_codex_login,
    a_fake_claude,
    a_grok_login,
    a_private_root,
)
from tests.support.service_cli_runner_support import (
    drive_over_http,
    eventually,
    post_a_run,
    shown,
    status_of,
)
from tests.support.service_support import (
    LISTED_MODEL,
    PINNED_CLAUDE_VERSION,
    SONGMAKER_ORIGIN,
    TEST_ISSUER,
    _service_config_document,
    _toml_lines,
    process_token_file,
)

from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.codex.run import CODEX_CLI_PROCESS_POOL
from agent_runtime.grok.run import GROK_CLI_PROCESS_POOL
from agent_runtime_client.wire import FailureCode, RunEvent, RunState, RunStatus, ToolCall
from agent_runtime_service.cli_runner import CliRunner, mcp_tls_context
from agent_runtime_service.config import Provider, ServiceConfig
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.runners import Runners

TURN_STDIN = "User: a\n\nAssistant: b\n\nUser: c"

CANARY = "cli-text-canary-3b71"

PARKED = "exec sleep 600"


@dataclass(frozen=True)
class FailingRun:
    """A stand-in program's stream and exit, and the closed cause the service logs for it."""

    case: str
    stream: tuple[bytes, ...]
    then: str
    failure_class: str


@dataclass(frozen=True)
class ProviderTable:
    provider: Provider
    model: str
    pin: str
    login_name: str
    version_line: str
    preconditions_marker: str
    """The file the stand-in leaves once the run it was given passed its guards."""
    pool: CliProcessPool
    login_document: Callable[[str | None], bytes]
    """The sound login; given an access token, the same login holding that token."""
    stand_in: Callable[..., Path]
    """The service-side stand-in: the CLI's fake with the turn input and version line bound."""
    relay_stand_in: Callable[..., Path]
    """The stand-in of a relay run: the CLI's relay-calling fake with its version line bound."""
    answering: Callable[[str, bool], tuple[bytes, ...]]
    """The stand-in's stream for an answer, complete or cut before its end."""
    access_canary: str
    """The secret the child's copy of the login holds (the one that is not blanked)."""
    canaries: tuple[str, ...]
    refresh_canary: str
    """The renewal secret the installed login holds and no child or log ever does."""
    not_a_document: str
    incomplete: str
    malformed_login: bytes
    api_key_login: bytes
    empty_token_login: bytes
    keyless_login: bytes
    foreign_login: bytes
    """A sound login of the other table: no sign-in of this one."""
    failing_runs: tuple[FailingRun, ...]
    default_settings: Mapping[str, Any]
    """What the service's run file holds when the operator sets nothing."""
    relay_run_default_settings: Mapping[str, Any]
    """The same file for a run attached to a relay."""
    refused_run: FailingRun
    """What the program prints and does when it refuses a mistyped setting."""

    @property
    def listed(self) -> str:
        return f"{self.provider}/{self.model}"

    def login_file(self, directory: Path, contents: bytes | None = None) -> Path:
        return write_login(
            directory / self.login_name, self.login_document(None) if contents is None else contents
        )

    def service_fake(
        self,
        directory: Path,
        stream: tuple[bytes, ...] | None = None,
        then: str = "exit 0",
        **kw: Any,
    ) -> Path:
        return self.stand_in(
            directory,
            self.answering("done", True) if stream is None else stream,
            then,
            **kw,
        )

    def document(
        self,
        tmp_path: Path,
        binary: Path,
        login_file: Path,
        *,
        runs: dict[str, object] | None = None,
        songmaker: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        document = _service_config_document(TEST_ISSUER, runs, songmaker)
        if songmaker is None:
            del document["apps"]["songmaker"]["mcp_origin"]
        document["defaults"] = {"model": self.listed}
        document["cli"] = {
            "working_root": str(a_private_root(tmp_path)),
            self.provider.value: {
                "binary": str(binary),
                "version": self.pin,
                "models": [self.model],
            },
        }
        document["cli"][self.provider]["token_file"] = str(login_file)
        return document

    def document_with_both(
        self, tmp_path: Path, stream: tuple[bytes, ...] | None = None
    ) -> dict[str, Any]:
        """Claude and this table, each with a stand-in program of its own under ``tmp_path``."""
        claude_directory = tmp_path / "claude-directory"
        claude_directory.mkdir()
        own = self.document(
            tmp_path, self.service_fake(tmp_path, stream), self.login_file(tmp_path)
        )
        return with_claude_beside(own, a_fake_claude(claude_directory))


CODEX_VERSION_LINE = "codex-cli 0.163.0"

GROK_VERSION_LINE = "grok 1.0.60 (01a8e1c591c0)"

_GROK_REALM = next(iter(a_grok_login().values()))

GROK_KEY = _GROK_REALM["key"]

GROK_REFRESH_CANARY = _GROK_REALM["refresh_token"]


def a_grok_login_with(key: str | None = None) -> bytes:
    login = a_grok_login()
    if key is not None:
        next(iter(login.values()))["key"] = key
    return json.dumps(login).encode()


def a_codex_login_with(access_token: str | None = None) -> bytes:
    return a_codex_login(tokens=None if access_token is None else {"access_token": access_token})


def a_codex_stream(text: str, finished: bool) -> tuple[bytes, ...]:
    return (THREAD, a_message(text), *((DONE,) if finished else ()))


def _refused_by_the_provider() -> bytes:
    return codex_line(
        type="turn.failed",
        error={"message": f"unexpected status 401 Unauthorized: {CANARY}"},
    )


CODEX_TABLE = ProviderTable(
    provider=Provider.OPENAI,
    model="gpt-5.5",
    pin="0.163.0",
    login_name="codex-login.json",
    version_line=CODEX_VERSION_LINE,
    preconditions_marker=CODEX_PRECONDITIONS_MARKER,
    pool=CODEX_CLI_PROCESS_POOL,
    login_document=a_codex_login_with,
    stand_in=partial(
        a_fake_codex,
        system_text=SYSTEM_TEXT,
        expected_stdin=TURN_STDIN,
        version_line=CODEX_VERSION_LINE,
    ),
    relay_stand_in=partial(a_relay_calling_codex, version_line=CODEX_VERSION_LINE),
    answering=a_codex_stream,
    access_canary=CANARY_ACCESS_TOKEN,
    canaries=(CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN),
    refresh_canary=json.loads(a_codex_login())["tokens"]["refresh_token"],
    not_a_document="is not a Codex login document",
    incomplete=(
        "is not a complete Codex sign-in; an API key (OPENAI_API_KEY) is not a subscription login"
    ),
    malformed_login=a_codex_login(tokens={"access_token": 5}),
    api_key_login=json.dumps({"OPENAI_API_KEY": CANARY_ACCESS_TOKEN}).encode(),
    empty_token_login=a_codex_login(tokens={"id_token": ""}),
    keyless_login=json.dumps({"auth_mode": "chatgpt"}).encode(),
    foreign_login=a_grok_login_with(),
    default_settings=CODEX_DEFAULT_SETTINGS,
    relay_run_default_settings=CODEX_RELAY_RUN_DEFAULT_SETTINGS,
    refused_run=FailingRun(
        "refused-setting", (), f"echo {CANARY} >&2; exit 1", "CodexAnswerMissing"
    ),
    failing_runs=(
        FailingRun(
            "failed-turn-with-a-bare-401",
            (codex_line(type="turn.failed", error={"message": f"401 {CANARY}"}),),
            "exit 1",
            "CodexTurnFailed",
        ),
        FailingRun(
            "login-refused", (_refused_by_the_provider(),), "exit 1", "CliLoginRefusedError"
        ),
        FailingRun(
            "unknown-item",
            (an_item("item.started", "new_kind", text=CANARY),),
            "exit 0",
            "CodexUnknownStreamPart",
        ),
        FailingRun(
            "tool-item-then-login-refused",
            (
                an_item("item.started", "command_execution", command=CANARY),
                _refused_by_the_provider(),
            ),
            "exit 1",
            "CliLoginRefusedError",
        ),
    ),
)


def a_grok_stream(text: str, finished: bool) -> tuple[bytes, ...]:
    return (a_text(text), *((END,) if finished else ()))


GROK_TABLE = ProviderTable(
    provider=Provider.XAI,
    model="grok-4.6",
    pin="1.0.60",
    login_name="grok-login.json",
    version_line=GROK_VERSION_LINE,
    preconditions_marker=GROK_PRECONDITIONS_MARKER,
    pool=GROK_CLI_PROCESS_POOL,
    login_document=a_grok_login_with,
    stand_in=partial(
        a_fake_grok,
        system_text=SYSTEM_TEXT,
        expected_prompt=TURN_STDIN,
        version_line=GROK_VERSION_LINE,
    ),
    relay_stand_in=partial(a_relay_calling_grok, version_line=GROK_VERSION_LINE),
    answering=a_grok_stream,
    access_canary=GROK_KEY,
    canaries=(GROK_KEY, GROK_REFRESH_CANARY),
    refresh_canary=GROK_REFRESH_CANARY,
    not_a_document="is not a Grok login document",
    incomplete=(
        "is not a complete Grok sign-in; an API key (XAI_API_KEY) is not a subscription login"
    ),
    malformed_login=json.dumps({"xai.example": {"key": 5}}).encode(),
    api_key_login=json.dumps({"XAI_API_KEY": GROK_KEY}).encode(),
    empty_token_login=a_grok_login_with(""),
    keyless_login=json.dumps({"xai.example": {"refresh_token": GROK_REFRESH_CANARY}}).encode(),
    foreign_login=a_codex_login(),
    default_settings={},
    relay_run_default_settings={},
    refused_run=FailingRun(
        "refused-setting",
        (),
        f"echo 'Error: Failed to create agent config: {CANARY}' >&2; echo 'in `ui.x`' >&2; exit 1",
        "GrokAnswerMissing",
    ),
    failing_runs=(
        FailingRun(
            "failed-turn-with-a-bare-401",
            (grok_line(type="error", message=f"401 {CANARY}"),),
            "exit 1",
            "GrokTurnFailed",
        ),
        FailingRun(
            "login-refused",
            (grok_line(type="error", message=f"Not signed in. {CANARY}"),),
            "exit 1",
            "CliLoginRefusedError",
        ),
        FailingRun(
            "unknown-event",
            (grok_line(type="surprise", data=CANARY),),
            "exit 0",
            "GrokUnknownStreamPart",
        ),
        FailingRun(
            "tool-call-before-login-refused",
            (
                grok_line(type="tool_call", title=CANARY),
                grok_line(type="error", message=f"Not signed in. {CANARY}"),
            ),
            "exit 1",
            "GrokToolCalled",
        ),
    ),
)

TABLES = (CODEX_TABLE, GROK_TABLE)


@pytest.fixture(name="table", params=TABLES, ids=lambda table: table.provider.value)
def the_table(request: pytest.FixtureRequest) -> ProviderTable:
    return request.param


CODEX_MODEL = CODEX_TABLE.model

CODEX_NAME = CODEX_TABLE.listed

a_codex_login_file = CODEX_TABLE.login_file

a_service_fake = CODEX_TABLE.service_fake

codex_document = CODEX_TABLE.document


@pytest.fixture(name="served_runs")
def the_served_runs(
    monkeypatch: pytest.MonkeyPatch, logging_state_restored: None
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: runs.append(where))
    return runs


def write_login(path: Path, contents: bytes, mode: int = 0o600) -> Path:
    """Write the login beside the file and move it over the file, as ``mv -T`` does."""
    beside = path.with_name(path.name + ".new")
    beside.write_bytes(contents)
    beside.chmod(mode)
    beside.replace(path)
    return path


def with_claude_beside(document: dict[str, Any], claude: Path) -> dict[str, Any]:
    document["cli"][Provider.ANTHROPIC] = {
        "binary": str(claude),
        "version": PINNED_CLAUDE_VERSION,
        "models": [LISTED_MODEL],
        "token_file": str(process_token_file()),
    }
    return document


def a_codex_config(document: dict[str, Any]) -> ServiceConfig:
    return ServiceConfig.model_validate(document)


def a_config_with_both_providers(
    tmp_path: Path, table: ProviderTable = CODEX_TABLE
) -> ServiceConfig:
    """``service_config`` plus a listed table of ``table`` (Codex unless named)."""
    document = table.document_with_both(tmp_path)
    document["defaults"] = {"model": f"{Provider.ANTHROPIC}/{LISTED_MODEL}"}
    document["apps"]["songmaker"]["mcp_origin"] = SONGMAKER_ORIGIN
    return a_codex_config(document)


def written_document(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "agent-runtime-service.toml"
    path.write_text("\n".join(_toml_lines(document)) + "\n", encoding="utf-8")
    return path


def the_refusal_of(document: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as refused:
        a_codex_config(document)
    return str(refused.value)


def a_table_service(
    table: ProviderTable, tmp_path: Path, **options: Any
) -> tuple[ServiceConfig, Path]:
    """The table's config with a stand-in program that captures what it was started with."""
    login = table.login_file(tmp_path)
    capture = tmp_path / "capture"
    binary = table.service_fake(tmp_path, capture=capture, **options.pop("fake", {}))
    return a_codex_config(table.document(tmp_path, binary, login, **options)), capture


def argv_of(capture: Path) -> list[str]:
    return (capture / "argv").read_text().splitlines()


def with_levels(
    document: dict[str, Any], table: ProviderTable, agent: str, *levels: dict[str, Any]
) -> dict[str, Any]:
    """``document`` with the table's ``cli_settings`` at defaults, the app and ``agent``."""
    defaults, app, agent_level = levels
    key = table.provider.value
    document["defaults"]["cli_settings"] = {key: defaults}
    songmaker = document["apps"]["songmaker"]
    songmaker["cli_settings"] = {key: app}
    songmaker["agents"] = {agent: {"cli_settings": {key: agent_level}}}
    return document


def captured_settings(capture: Path) -> dict[str, Any]:
    return tomllib.loads((capture / "config.toml").read_text())


def a_save_note() -> ClaudeCall:
    return ClaudeCall("save_note", {"text": "la la"})


def the_app_tools() -> dict[str, str]:
    return {"save_note": "side_effecting", "read_note": "read_only"}


@dataclass(frozen=True)
class ToolRun:
    """One relay run of a table through ``POST /runs``: its frames, status, app and child."""

    events: list[RunEvent]
    status: RunStatus
    app: ToolApp
    capture: CodexChildCapture
    config: ServiceConfig

    @property
    def ending(self) -> tuple[RunState, FailureCode | None]:
        return self.status.state, self.status.failure_code

    @property
    def shape(self) -> list[tuple[str, str | None]]:
        return [
            (event.type, event.name if isinstance(event, ToolCall) else text)
            for event, (_type, text) in zip(self.events[1:], shown(self.events), strict=True)
        ]

    @property
    def everything_the_service_showed(self) -> str:
        frames = json.dumps([event.model_dump(mode="json") for event in self.events])
        return frames + self.status.model_dump_json()


def a_tool_run(
    table: ProviderTable,
    directory: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tools: Mapping[str, str] | None = None,
    runs: dict[str, object] | None = None,
    with_origin: bool = True,
    calls: Sequence[ClaudeCall] = (),
    **stand_in_options: Any,
) -> ToolRun:
    """One run of the real runner for an app with an MCP origin, over HTTP, naming ``tools``.

    ``tools`` defaults to ``the_app_tools()``; without ``with_origin`` the app has no origin.
    """
    named = the_app_tools() if tools is None else tools
    directory.mkdir(exist_ok=True)
    binary = table.relay_stand_in(directory, calls=calls, **stand_in_options)
    with a_tool_app(authority, list(the_app_tools())) as app:
        songmaker = {"mcp_origin": app.origin} if with_origin else None
        config = a_codex_config(
            table.document(
                directory, binary, table.login_file(directory), runs=runs, songmaker=songmaker
            )
        )
        runners = Runners(
            cli=CliRunner(config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock())
        )

        def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
            events = post_a_run(base_url, named)
            eventually(lambda: status_of(base_url, events).state is not RunState.RUNNING)
            return events, status_of(base_url, events)

        events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario, runners)
    return ToolRun(events, status, app, CodexChildCapture(directory / "capture"), config)
