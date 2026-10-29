"""The operator's Claude tools and settings reach a run through the service's CLI runner.

Rows serve the real app on a loopback socket and run a stand-in for the pinned CLI as a real
child that announces what the test scripts; the stand-in records its argument vector, its
environment and every file of its home. The service's sandbox default is Claude's user layer in
that home, and the operator's settings are the higher ``--settings`` layer.
"""

from __future__ import annotations

import json
import tomllib
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from tests.support.anchored_support import ChildCleanup
from tests.support.cli_tools_support import ClaudeCall, a_relay_calling_claude, a_tool_app
from tests.support.codex_support import DEFAULT_SETTINGS
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.provider_test_support import (
    SUCCESS_LINE,
    TEXT_LINE,
    a_fake_claude,
    an_init,
)
from tests.support.service_cli_runner_support import (
    THREE_MESSAGE_TURN,
    a_config,
    drive_over_http,
    headers,
    post_a_run,
    shown,
    status_of,
    trusting,
)
from tests.support.service_codex_support import (
    a_codex_config,
    a_codex_login_file,
    a_service_fake,
    codex_document,
)
from tests.support.service_support import (
    COWRITER,
    PINNED_CLAUDE_VERSION,
    decoded_events,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    post_run,
    run_body,
)

from agent_runtime_client.wire import FailureCode, RunEvent
from agent_runtime_service.cli_runner import CliRunner, mcp_tls_context
from agent_runtime_service.config import (
    AgentConfig,
    ClaudeCliSettingsConfig,
    CliSettingsConfig,
    ServiceConfig,
)
from agent_runtime_service.run_events import RUNNER_FAILED_MESSAGE
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.runners import Runners

SANDBOX_DEFAULT = {
    "sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}
}
SETTING_CANARY = "setting-canary-2e9b"
ANOTHER_AGENT = "critic"
TOOLS = {"save_note": "side_effecting", "read_note": "read_only"}


def configured_for(
    config: ServiceConfig,
    *,
    tools: list[str] | None = None,
    settings: dict[str, object] | None = None,
    app_name: str = "songmaker",
) -> ServiceConfig:
    """``config`` with the Claude table at the level of the named app's agent ``cowriter``."""
    level = CliSettingsConfig(anthropic=ClaudeCliSettingsConfig(tools=tools, settings=settings))
    app = config.apps[app_name]
    agents = {**app.agents, COWRITER: AgentConfig(cli_settings=level)}
    apps = {**config.apps, app_name: app.model_copy(update={"agents": agents})}
    return config.model_copy(update={"apps": apps})


def announcing(*tools: str) -> tuple[bytes, ...]:
    return (an_init(tools=list(tools)), TEXT_LINE, SUCCESS_LINE)


def argv_of(capture: Path) -> list[str]:
    return (capture / "argv").read_text().splitlines()


def environment_of(capture: Path) -> dict[str, str]:
    lines = (capture / "environment").read_text().splitlines()
    return dict(entry.split("=", 1) for entry in lines if "=" in entry)


def run_as(base_url: str, agent: str) -> list[RunEvent]:
    body = run_body(turn=THREE_MESSAGE_TURN, agent=agent)
    return decoded_events(post_run(base_url, headers(), body).content)


def test_a_listed_tool_runs_with_the_sandbox_default_as_the_user_layer_in_the_run_home(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    binary = a_fake_claude(tmp_path, stream=announcing("Bash"), capture=capture)
    config = configured_for(a_config(tmp_path, binary), tools=["Bash"])

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    argv = argv_of(capture)
    assert shown(events) == [("assistant_text", "hi"), ("succeeded", "done")]
    assert (
        argv[argv.index("--tools") + 1],
        argv[argv.index("--setting-sources") + 1],
        argv[-2:],
        "--settings" in argv,
    ) == ("Bash", "user", ["--allowedTools", "Bash"], False)
    assert environment_of(capture)["CLAUDE_CONFIG_DIR"] == environment_of(capture)["HOME"]
    assert json.loads((capture / "files" / "settings.json").read_text()) == SANDBOX_DEFAULT


def test_the_operators_settings_reach_the_run_as_the_settings_option_above_the_default(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    binary = a_fake_claude(tmp_path, stream=announcing("Bash"), capture=capture)
    operator = {"sandbox": {"enabled": False}, "outputStyle": SETTING_CANARY}
    config = configured_for(a_config(tmp_path, binary), tools=["Bash"], settings=operator)

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    argv = argv_of(capture)
    home = environment_of(capture)["HOME"]
    assert shown(events)[-1] == ("succeeded", "done")
    assert argv[argv.index("--settings") + 1] == f"{home}/cli-settings.json"
    assert json.loads((capture / "files" / "cli-settings.json").read_text()) == operator
    assert json.loads((capture / "files" / "settings.json").read_text()) == SANDBOX_DEFAULT
    assert SETTING_CANARY not in " ".join(argv)


@pytest.mark.parametrize(
    ("app_with_the_table", "agent"),
    [
        pytest.param("songmaker", ANOTHER_AGENT, id="another-agent"),
        pytest.param("presentator", COWRITER, id="another-app"),
    ],
)
def test_an_agent_without_the_table_runs_exactly_as_a_run_without_cli_settings(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    app_with_the_table: str,
    agent: str,
) -> None:
    capture = tmp_path / "capture"
    config = configured_for(
        a_config(tmp_path, a_fake_claude(tmp_path, capture=capture)),
        tools=["Bash"],
        settings={"outputStyle": "x"},
        app_name=app_with_the_table,
    )

    events = drive_over_http(child_cleanup, monkeypatch, config, lambda url: run_as(url, agent))

    argv = argv_of(capture)
    assert shown(events)[-1] == ("succeeded", "done")
    assert (
        argv[argv.index("--tools") + 1],
        argv[argv.index("--setting-sources") + 1],
        "--settings" in argv,
        "--allowedTools" in argv,
        sorted(path.name for path in (capture / "files").iterdir()),
        "CLAUDE_CONFIG_DIR" in environment_of(capture),
    ) == ("", "", False, False, ["system-prompt"], False)


def test_a_listed_tool_the_cli_does_not_announce_ends_the_run_and_the_log_names_it(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    binary = a_fake_claude(tmp_path, stream=announcing())
    config = configured_for(a_config(tmp_path, binary), tools=["Bash"])

    def scenario(base_url: str) -> tuple[list[RunEvent], FailureCode | None]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events).failure_code

    events, failure_code = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(events), failure_code) == (
        [("failed", RUNNER_FAILED_MESSAGE)],
        FailureCode.PROVIDER_ERROR,
    )
    assert [kind for _event, kind, _function in failure_lines(caplog)] == ["RunSurfaceRefused"]
    assert "LISTED_TOOL_NOT_ANNOUNCED" in caplog.text
    assert "A listed tool was not announced: Bash" in caplog.text


def test_a_relay_call_passes_beside_a_listed_tool_as_without_one(
    tmp_path: Path,
    authority: Any,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = a_relay_calling_claude(
        tmp_path,
        version=PINNED_CLAUDE_VERSION,
        calls=[ClaudeCall("save_note", {"text": "la la"})],
        builtin_tools=["Bash"],
    )
    with ExitStack() as running:
        app = running.enter_context(a_tool_app(authority, list(TOOLS)))
        config = configured_for(
            trusting(a_config(tmp_path, binary, songmaker={"mcp_origin": app.origin}), authority),
            tools=["Bash"],
        )
        runners = Runners(
            cli=CliRunner(config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock())
        )

        events = drive_over_http(
            child_cleanup, monkeypatch, config, lambda url: post_a_run(url, TOOLS), runners
        )

    assert [event.type for event in events] == [
        "run_started",
        "tool_call",
        "tool_result",
        "succeeded",
    ]
    assert [call.message["params"]["arguments"] for call in app.calls] == [{"text": "la la"}]
    capture = tmp_path / "capture"
    assert argv_of(capture)[-2:] == ["--allowedTools", "mcp__relay__*,Bash"]


def test_a_codex_run_gets_its_pinned_settings_whatever_the_claude_table_says(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    document = codex_document(
        tmp_path, a_service_fake(tmp_path, capture=capture), a_codex_login_file(tmp_path)
    )
    config = a_codex_config(document)
    table = CliSettingsConfig(
        anthropic=ClaudeCliSettingsConfig(tools=["Bash"], settings={"outputStyle": "x"})
    )
    config = config.model_copy(
        update={"defaults": config.defaults.model_copy(update={"cli_settings": table})}
    )

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    assert shown(events)[-1] == ("succeeded", "done")
    assert tomllib.loads((capture / "config.toml").read_text()) == DEFAULT_SETTINGS
