"""The service serves a Codex table: the run rows only Codex has.

Rows serve the real app on a loopback socket and run a stand-in for the pinned CLI as a real
child, with no installed core configuration; the rows every table has are those of
``test_cli_runner_tables``. The tool rows run a stand-in that talks to the run's relay and a TLS
fake app signed by a test CA. Test values: the Codex table with its login, the listed model
``gpt-5.5`` and the pinned version 0.163.0.
"""

from __future__ import annotations

import json
import math
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.support.anchored_support import ChildCleanup
from tests.support.cli_tools_support import (
    ClaudeCall,
    CodexChildCapture,
    ToolApp,
    a_relay_calling_codex,
    a_tool_app,
)
from tests.support.codex_support import (
    DEFAULT_SETTINGS,
    DONE,
    a_message,
    an_item,
    codex_line,
)
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.mcp_app_fake import TestAuthority
from tests.support.provider_test_support import (
    a_fake_claude,
)
from tests.support.service_cli_runner_support import (
    a_held_reap,  # noqa: F401 - the fixture is named in its decorator
    drive_over_http,
    eventually,
    post_a_run,
    private_root_of,
    shown,
    status_of,
)
from tests.support.service_support import (
    COWRITER,
    LISTED_MODEL,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
)
from tests.support.service_table_support import (
    CANARY,
    CODEX_MODEL,
    CODEX_TABLE,
    a_codex_config,
    a_codex_login_file,
    a_service_fake,
    a_table_service,
    argv_of,
    captured_settings,
    codex_document,
    with_claude_beside,
    with_levels,
)

import agent_runtime_service.cli_runner as cli_runner
from agent_runtime.codex.run import RELAY_TOKEN_VARIABLE
from agent_runtime_client.wire import (
    FailureCode,
    RunEvent,
    RunFile,
    RunState,
    RunStatus,
    Succeeded,
    ToolCall,
    ToolResult,
)
from agent_runtime_service.capability import Capability
from agent_runtime_service.cli_runner import CliRunner, mcp_tls_context
from agent_runtime_service.config import Provider, ServiceConfig
from agent_runtime_service.run_events import (
    INVALID_EVENT_MESSAGE,
    TOO_MUCH_DATA_MESSAGE,
)
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.run_tools import RunTools
from agent_runtime_service.runners import AdmittedRun, Runners

# Runs through ``POST /runs`` with the stand-in CLI.


def a_codex_service(tmp_path: Path, **options: Any) -> tuple[ServiceConfig, Path]:
    return a_table_service(CODEX_TABLE, tmp_path, **options)


def test_a_codex_run_without_words_or_a_file_ends_as_an_invalid_event_not_a_runner_failure(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    turn_without_words = (codex_line(type="turn.started"), DONE)
    config, _capture = a_codex_service(tmp_path, fake={"stream": turn_without_words})

    def scenario(base_url: str) -> Any:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(events), status.failure_code, failure_lines(caplog)) == (
        [("failed", INVALID_EVENT_MESSAGE)],
        FailureCode.PROVIDER_ERROR,
        [],
    )


@pytest.mark.parametrize(
    ("size", "ending"),
    [
        pytest.param(1024, ("succeeded", "done"), id="exactly-the-limit"),
        pytest.param(1025, ("failed", TOO_MUCH_DATA_MESSAGE), id="one-byte-over"),
    ],
)
def test_a_codex_run_is_held_to_the_configured_files_limit(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
    ending: tuple[str, str],
) -> None:
    config, _capture = a_codex_service(
        tmp_path,
        runs={"max_files_bytes": 1024},
        fake={"then": f"truncate -s {size} output/note"},
    )

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    assert shown(events)[-1] == ending
    assert list(private_root_of(config).iterdir()) == []


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param((codex_line(type="turn.started"), DONE), id="no-items"),
        pytest.param(
            (
                codex_line(type="turn.started"),
                an_item("item.completed", "command_execution", command=CANARY),
                DONE,
            ),
            id="tool-items",
        ),
    ],
)
def test_a_codex_run_without_words_succeeds_beside_a_file(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    stream: tuple[bytes, ...],
) -> None:
    config, _capture = a_codex_service(
        tmp_path, fake={"stream": stream, "then": "printf abc > output/a"}
    )

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    terminal = events[-1]
    assert isinstance(terminal, Succeeded)
    assert (terminal.text, terminal.files) == ("", (RunFile(name="a", content=b"abc"),))


def test_codex_tool_items_reach_no_frame_status_or_log_and_the_answer_succeeds(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    stream = (
        an_item("item.started", "command_execution", command=CANARY),
        an_item("item.completed", "command_execution", command=CANARY, aggregated_output=CANARY),
        an_item("item.completed", "web_search", query=CANARY),
        a_message("done"),
        DONE,
    )
    config, _capture = a_codex_service(tmp_path, fake={"stream": stream})

    def scenario(base_url: str) -> Any:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert shown(events) == [("assistant_text", "done"), ("succeeded", "done")]
    seen = f"{[e.model_dump_json() for e in events]} {status.model_dump_json()} {caplog.text}"
    assert CANARY not in seen


# A Codex tool run takes the app's tools through the run's relay.

SAVE_NOTE = ClaudeCall("save_note", {"text": "la la"})
TOOLS = {"save_note": "side_effecting", "read_note": "read_only"}
PROVIDER_ERROR = (RunState.FAILED, FailureCode.PROVIDER_ERROR)
SUCCEEDED = (RunState.SUCCEEDED, None)


@dataclass(frozen=True)
class CodexToolRun:
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


def a_codex_tool_run(
    directory: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tools: Mapping[str, str] = TOOLS,
    runs: dict[str, object] | None = None,
    **codex_options: Any,
) -> CodexToolRun:
    """One run of the real runner for an app with an MCP origin, over HTTP, naming ``tools``."""
    directory.mkdir(exist_ok=True)
    binary = a_relay_calling_codex(
        directory, version_line=CODEX_TABLE.version_line, **codex_options
    )
    with a_tool_app(authority, list(TOOLS)) as app:
        document = codex_document(
            directory,
            binary,
            a_codex_login_file(directory),
            runs=runs,
            songmaker={"mcp_origin": app.origin},
        )
        config = a_codex_config(document)
        runners = Runners(
            cli=CliRunner(config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock())
        )

        def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
            events = post_a_run(base_url, tools)
            eventually(lambda: status_of(base_url, events).state is not RunState.RUNNING)
            return events, status_of(base_url, events)

        events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario, runners)
    return CodexToolRun(events, status, app, CodexChildCapture(directory / "capture"), config)


def test_a_codex_relay_call_reaches_the_wire_under_the_apps_name_and_codexs_own_lines_do_not(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")

    run = a_codex_tool_run(
        tmp_path, authority, child_cleanup, monkeypatch, calls=[SAVE_NOTE], stray_text=CANARY
    )

    assert run.shape == [
        ("tool_call", "save_note"),
        ("tool_result", None),
        ("assistant_text", "done"),
        ("succeeded", "done"),
    ]
    [result] = [event for event in run.events if isinstance(event, ToolResult)]
    assert (result.content, result.is_error) == ("save_note", False)
    assert [call.message["params"]["arguments"] for call in run.app.calls] == [{"text": "la la"}]
    assert CANARY not in run.everything_the_service_showed + caplog.text


def test_a_codex_tool_runs_settings_file_holds_the_relay_it_was_given_over_the_defaults(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = a_codex_tool_run(tmp_path, authority, child_cleanup, monkeypatch, calls=[SAVE_NOTE])

    settings = captured_settings(run.capture.directory / "files")
    entry = settings["mcp_servers"].pop("relay")
    assert settings == DEFAULT_SETTINGS
    assert (entry["tool_timeout_sec"], entry["bearer_token_env_var"]) == (
        math.ceil(run.config.runs.max_duration_s),
        RELAY_TOKEN_VARIABLE,
    )
    assert [reply["status"] for reply in run.capture.listing_replies] == [200]


def test_the_relay_secret_reaches_codex_only_in_its_environment_and_the_capability_never(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")

    run = a_codex_tool_run(tmp_path, authority, child_cleanup, monkeypatch, calls=[SAVE_NOTE])

    secret, capability = run.capture.relay_secret, run.app.capability
    environment = run.capture.text_of("environment").splitlines()
    assert (len(secret) > 16, capability.count(".")) == (True, 2)
    assert [line for line in environment if secret in line] == [
        f"{RELAY_TOKEN_VARIABLE}={secret}"
    ]
    given = [run.capture.text_of("argv"), run.capture.stdin, *run.capture.run_files.values()]
    assert [part for part in given if secret in part] == []
    assert capability not in run.capture.everything_it_was_given
    shown_by_the_service = run.everything_the_service_showed + caplog.text
    assert (secret in shown_by_the_service, capability in shown_by_the_service) == (False, False)


def test_a_codex_call_past_the_tool_budget_never_reaches_the_app(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = a_codex_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE, SAVE_NOTE],
        runs={"max_tool_calls": 1},
    )

    assert (run.ending, len(run.app.calls), len(run.capture.replies)) == (
        (RunState.FAILED, FailureCode.LIMIT_EXCEEDED),
        1,
        1,
    )


@pytest.mark.parametrize(
    ("options", "ending", "app_calls"),
    [
        pytest.param(
            {"calls": [SAVE_NOTE], "wrong_bearer": "not-the-relays-secret"},
            PROVIDER_ERROR,
            0,
            id="relay-refuses-the-bearer",
        ),
        pytest.param({"lists": "never"}, PROVIDER_ERROR, 0, id="answers-without-the-relay"),
        pytest.param(
            {"lists": "after-text", "calls": [SAVE_NOTE]},
            SUCCEEDED,
            1,
            id="lists-after-its-first-text",
        ),
    ],
)
def test_a_codex_tool_run_ends_provider_error_unless_its_relay_listed_and_one_child_ran(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    ending: tuple[RunState, FailureCode | None],
    app_calls: int,
) -> None:
    closes: list[None] = []
    close = RunTools.close

    async def counted(tools: RunTools, reap: Callable[[], Any]) -> None:
        closes.append(None)
        await close(tools, reap)

    monkeypatch.setattr(RunTools, "close", counted)

    run = a_codex_tool_run(tmp_path, authority, child_cleanup, monkeypatch, **options)

    assert (run.ending, len(run.app.calls), len(child_cleanup.listed()), len(closes)) == (
        ending,
        app_calls,
        1,
        1,
    )
    assert ("succeeded" in [kind for kind, _ in run.shape]) is (ending == SUCCEEDED)


def test_a_tool_free_codex_run_on_an_app_with_an_origin_gets_no_relay_and_the_same_argv(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def homeless_argv(run: CodexToolRun) -> list[str]:
        home = next(
            line.removeprefix("CODEX_HOME=")
            for line in run.capture.text_of("environment").splitlines()
            if line.startswith("CODEX_HOME=")
        )
        return [line.replace(home, "<home>") for line in run.capture.text_of("argv").split("\n")]

    with_tools = a_codex_tool_run(tmp_path / "tools", authority, child_cleanup, monkeypatch)
    tool_free = a_codex_tool_run(
        tmp_path / "free", authority, child_cleanup, monkeypatch, tools={}, lists="never"
    )

    assert (tool_free.ending, tool_free.app.fake.accepted_connections) == (SUCCEEDED, 0)
    assert captured_settings(tool_free.capture.directory / "files") == DEFAULT_SETTINGS
    assert RELAY_TOKEN_VARIABLE not in tool_free.capture.text_of("environment")
    assert homeless_argv(tool_free) == homeless_argv(with_tools)


# No run adapter factory sees the admitted run.


def test_no_run_adapter_factory_sees_the_admitted_run_or_its_capability() -> None:
    annotations = [
        hint
        for factory in (*cli_runner.RUN_ADAPTERS.values(), cli_runner.RunInputs)
        for hint in typing.get_type_hints(factory).values()
    ]

    assert [t for t in annotations if t in (AdmittedRun, Capability)] == []


# The operator's ``cli_settings`` reach the run as its settings file.


def test_an_operators_sandbox_mode_at_every_level_reaches_the_run_beside_the_services_arguments(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = a_codex_login_file(tmp_path)
    capture = tmp_path / "capture"
    binary = a_service_fake(tmp_path, capture=capture)
    levels = (
        {"sandbox_mode": "danger-full-access"},
        {"sandbox_mode": "workspace-write"},
        {"sandbox_mode": "workspace-write", "approval_policy": "on-request"},
    )
    document = with_levels(codex_document(tmp_path, binary, login), CODEX_TABLE, COWRITER, *levels)

    events = drive_over_http(child_cleanup, monkeypatch, a_codex_config(document), post_a_run)

    argv = argv_of(capture)
    assert shown(events)[-1] == ("succeeded", "done")
    assert captured_settings(capture) == {
        **DEFAULT_SETTINGS,
        "sandbox_mode": "workspace-write",
        "approval_policy": "on-request",
    }
    assert [a for a in argv if a.startswith("--model")] == [f"--model={CODEX_MODEL}"]
    assert ("--ephemeral" in argv, any(a.startswith("model_instructions_file=") for a in argv)) == (
        True,
        True,
    )


def claude_argv_of_a_run(
    directory: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    openai_settings: dict[str, Any],
) -> list[str]:
    directory.mkdir()
    claude_directory = directory / "claude-directory"
    claude_directory.mkdir()
    capture = directory / "capture"
    codex = codex_document(directory, a_service_fake(directory), a_codex_login_file(directory))
    document = with_claude_beside(codex, a_fake_claude(claude_directory, capture=capture))
    document["apps"]["songmaker"]["model"] = f"{Provider.ANTHROPIC}/{LISTED_MODEL}"
    if openai_settings:
        document["defaults"]["cli_settings"] = {"openai": openai_settings}
        document["apps"]["songmaker"]["cli_settings"] = {"openai": openai_settings}

    events = drive_over_http(child_cleanup, monkeypatch, a_codex_config(document), post_a_run)

    assert shown(events)[-1] == ("succeeded", "done")
    home = next(
        line.removeprefix("HOME=")
        for line in (capture / "environment").read_text().splitlines()
        if line.startswith("HOME=")
    )
    return [line.replace(home, "<home>") for line in argv_of(capture)]


def test_a_claude_run_gets_the_same_arguments_whatever_the_codex_settings_say(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    without = claude_argv_of_a_run(tmp_path / "without", child_cleanup, monkeypatch, {})
    with_settings = claude_argv_of_a_run(
        tmp_path / "with", child_cleanup, monkeypatch, {"web_search": "live"}
    )

    assert with_settings == without
