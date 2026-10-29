"""The service serves a Codex table: the run rows only Codex has.

Rows serve the real app on a loopback socket and run a stand-in for the pinned CLI as a real
child, with no installed core configuration; the rows every table has are those of
``test_cli_runner_tables``. Test values: the Codex table with its login, the listed model
``gpt-5.5`` and the pinned version 0.163.0.
"""

from __future__ import annotations

import tomllib
import typing
from pathlib import Path
from typing import Any

import pytest
from tests.support.anchored_support import ChildCleanup
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
from tests.support.provider_test_support import (
    a_fake_claude,
)
from tests.support.service_cli_runner_support import (
    a_held_reap,  # noqa: F401 - the fixture is named in its decorator
    drive_over_http,
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
    codex_document,
    with_claude_beside,
)

import agent_runtime_service.cli_runner as cli_runner
from agent_runtime_client.wire import (
    FailureCode,
    RunFile,
    Succeeded,
)
from agent_runtime_service.capability import Capability
from agent_runtime_service.config import Provider, ServiceConfig
from agent_runtime_service.run_events import (
    INVALID_EVENT_MESSAGE,
    RUNNER_FAILED_MESSAGE,
    TOO_MUCH_DATA_MESSAGE,
)
from agent_runtime_service.runners import AdmittedRun

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


# Tools are refused at admission until the relay serves Codex.


def test_no_run_adapter_factory_sees_the_admitted_run_or_its_capability() -> None:
    annotations = [
        hint
        for factory in (*cli_runner.RUN_ADAPTERS.values(), cli_runner.RunInputs)
        for hint in typing.get_type_hints(factory).values()
    ]

    assert [t for t in annotations if t in (AdmittedRun, Capability)] == []


# The operator's ``cli_settings`` reach the run as its settings file.

LEVEL_SETTINGS = (
    {"web_search": "live", "features": False},
    {"features": {"plugins": False, "hooks": True}},
    {"features": {"image_generation": True}},
)
SETTING_CANARY = "setting-canary-6d02"


def with_levels(document: dict[str, Any], agent: str, *levels: dict[str, Any]) -> dict[str, Any]:
    defaults, app, agent_level = levels
    document["defaults"]["cli_settings"] = {"openai": defaults}
    songmaker = document["apps"]["songmaker"]
    songmaker["cli_settings"] = {"openai": app}
    songmaker["agents"] = {agent: {"cli_settings": {"openai": agent_level}}}
    return document


def captured_settings(capture: Path) -> dict[str, Any]:
    return tomllib.loads((capture / "config.toml").read_text())


@pytest.mark.parametrize(
    ("agent", "features"),
    [
        pytest.param(
            COWRITER,
            {"plugins": False, "hooks": True, "image_generation": True},
            id="the-agent-with-a-table",
        ),
        pytest.param(
            "another-agent",
            {"plugins": False, "hooks": True},
            id="an-agent-without-one",
        ),
    ],
)
def test_the_three_levels_reach_the_run_as_one_merged_file_only_the_service_wrote(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    features: dict[str, bool],
) -> None:
    login = a_codex_login_file(tmp_path)
    capture = tmp_path / "capture"
    binary = a_service_fake(tmp_path, capture=capture)
    document = with_levels(codex_document(tmp_path, binary, login), agent, *LEVEL_SETTINGS)

    events = drive_over_http(child_cleanup, monkeypatch, a_codex_config(document), post_a_run)

    assert shown(events)[-1] == ("succeeded", "done")
    assert captured_settings(capture) == {
        **DEFAULT_SETTINGS,
        "web_search": "live",
        "features": features,
    }


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
    document = with_levels(codex_document(tmp_path, binary, login), COWRITER, *levels)

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


def test_a_run_without_cli_settings_gets_the_defaults_and_answers_as_before(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, capture = a_codex_service(tmp_path)

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    assert (shown(events), captured_settings(capture)) == (
        [("assistant_text", "done"), ("succeeded", "done")],
        DEFAULT_SETTINGS,
    )


def test_a_run_the_program_refuses_ends_closed_and_no_setting_or_program_text_reaches_a_frame(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    login = a_codex_login_file(tmp_path)
    binary = a_service_fake(tmp_path, stream=(), then=f"echo {CANARY} >&2; exit 1")
    document = codex_document(tmp_path, binary, login)
    document["defaults"]["cli_settings"] = {"openai": {"invented_key": SETTING_CANARY}}

    def scenario(base_url: str) -> Any:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, a_codex_config(document), scenario)

    assert (shown(events), status.failure_code) == (
        [("failed", RUNNER_FAILED_MESSAGE)],
        FailureCode.PROVIDER_ERROR,
    )
    assert [kind for _event, kind, _function in failure_lines(caplog)] == ["CodexAnswerMissing"]
    seen = f"{[e.model_dump_json() for e in events]} {status.model_dump_json()} {caplog.text}"
    assert (CANARY in seen, SETTING_CANARY in seen) == (False, False)


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
