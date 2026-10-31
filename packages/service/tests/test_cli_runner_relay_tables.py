"""The service attaches each table's run to the run's tool relay: the rows every table has.

Rows serve the real app on a loopback socket and run each table's relay-calling stand-in as a
real child, with no installed core configuration, against a TLS fake app signed by a test CA;
the builders are those of ``tests.support.service_table_support``. The rows only Grok's stream
has close the module; Codex's own facts stay in ``test_cli_runner_codex_profile``.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from tests.support.anchored_support import ChildCleanup
from tests.support.cli_tools_support import relay_refuses
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.mcp_app_fake import TestAuthority
from tests.support.provider_test_support import held_slots
from tests.support.service_cli_runner_support import eventually, private_root_of
from tests.support.service_support import (
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
)
from tests.support.service_table_support import (
    CANARY,
    GROK_TABLE,
    ProviderTable,
    a_save_note,
    a_tool_run,
    captured_settings,
    the_table,  # noqa: F401 - the fixture is named in its signature
)

from agent_runtime.cli_run import RELAY_TOKEN_VARIABLE
from agent_runtime_client.wire import FailureCode, RunState, ToolResult

PROVIDER_ERROR = (RunState.FAILED, FailureCode.PROVIDER_ERROR)


def test_a_relay_call_reaches_the_wire_under_the_apps_name_and_the_clis_own_lines_do_not(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    table: ProviderTable,
) -> None:
    caplog.set_level("DEBUG")

    run = a_tool_run(
        table,
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[a_save_note()],
        stray_text=CANARY,
    )

    assert run.shape == [
        ("tool_call", "save_note"),
        ("tool_result", None),
        ("assistant_text", "done"),
        ("succeeded", "done"),
    ]
    [result] = [event for event in run.events if isinstance(event, ToolResult)]
    assert (result.content, result.is_error) == ("save_note", False)
    assert [call.message["params"]["name"] for call in run.app.calls] == ["save_note"]
    assert [call.message["params"]["arguments"] for call in run.app.calls] == [{"text": "la la"}]
    assert CANARY not in run.everything_the_service_showed + caplog.text


def test_a_tool_runs_settings_file_holds_the_relay_it_was_given_over_the_defaults(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    table: ProviderTable,
) -> None:
    run = a_tool_run(table, tmp_path, authority, child_cleanup, monkeypatch, calls=[a_save_note()])

    settings = captured_settings(run.capture.directory / "files")
    entry = run.capture.relay
    assert settings == {**table.relay_run_default_settings, "mcp_servers": {"relay": entry}}
    assert (entry["tool_timeout_sec"], entry["bearer_token_env_var"]) == (
        math.ceil(run.config.runs.max_duration_s),
        RELAY_TOKEN_VARIABLE,
    )
    assert [reply["status"] for reply in run.capture.listing_replies] == [200]


def test_the_relay_secret_reaches_the_cli_only_in_its_environment_and_the_capability_never(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    table: ProviderTable,
) -> None:
    caplog.set_level("DEBUG")

    run = a_tool_run(table, tmp_path, authority, child_cleanup, monkeypatch, calls=[a_save_note()])

    secret, capability = run.capture.relay_secret, run.app.capability
    environment = run.capture.text_of("environment").splitlines()
    assert (len(secret) > 16, capability.count(".")) == (True, 2)
    assert [line for line in environment if secret in line] == [f"{RELAY_TOKEN_VARIABLE}={secret}"]
    given = [run.capture.text_of("argv"), run.capture.stdin, *run.capture.run_files.values()]
    assert [part for part in given if secret in part] == []
    assert capability not in run.capture.everything_it_was_given
    shown_by_the_service = run.everything_the_service_showed + caplog.text
    assert (secret in shown_by_the_service, capability in shown_by_the_service) == (False, False)


def test_a_call_past_the_tool_budget_never_reaches_the_app(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    table: ProviderTable,
) -> None:
    run = a_tool_run(
        table,
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[a_save_note(), a_save_note()],
        runs={"max_tool_calls": 1},
    )

    assert (run.ending, len(run.app.calls), len(run.capture.replies)) == (
        (RunState.FAILED, FailureCode.LIMIT_EXCEEDED),
        1,
        1,
    )


@pytest.mark.parametrize(
    ("options", "ending", "app_calls", "initialize_statuses"),
    [
        pytest.param(
            {"calls": [a_save_note()], "wrong_bearer": "not-the-relays-secret"},
            PROVIDER_ERROR,
            0,
            [401],
            id="relay-refuses-the-bearer",
        ),
        pytest.param({"lists": "never"}, PROVIDER_ERROR, 0, [], id="answers-without-the-relay"),
        pytest.param(
            {"lists": "after-text", "calls": [a_save_note()]},
            (RunState.SUCCEEDED, None),
            1,
            [200],
            id="lists-after-its-first-text",
        ),
    ],
)
def test_a_tool_run_ends_provider_error_unless_its_relay_listed_and_one_child_ran(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    table: ProviderTable,
    options: dict[str, Any],
    ending: tuple[RunState, FailureCode | None],
    app_calls: int,
    initialize_statuses: list[int],
) -> None:
    run = a_tool_run(table, tmp_path, authority, child_cleanup, monkeypatch, **options)

    assert (run.ending, len(run.app.calls), len(child_cleanup.listed())) == (
        ending,
        app_calls,
        1,
    )
    assert [reply["status"] for reply in run.capture.replies_to("initialize")] == (
        initialize_statuses
    )
    assert ("succeeded" in [kind for kind, _ in run.shape]) is (
        ending == (RunState.SUCCEEDED, None)
    )
    eventually(lambda: relay_refuses(run.capture.relay_port, 10.0))
    assert (list(private_root_of(run.config).iterdir()), held_slots(table.pool)) == ([], 0)


def test_a_tool_free_run_on_an_app_with_an_origin_is_the_run_of_an_app_without_one(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    table: ProviderTable,
) -> None:
    tool_free, without_origin = (
        a_tool_run(
            table,
            tmp_path / name,
            authority,
            child_cleanup,
            monkeypatch,
            tools={},
            with_origin=with_origin,
            lists="never",
        )
        for name, with_origin in (("tool-free", True), ("without-origin", False))
    )

    assert (tool_free.ending, tool_free.app.fake.accepted_connections) == (
        (RunState.SUCCEEDED, None),
        0,
    )
    assert captured_settings(tool_free.capture.directory / "files") == table.default_settings
    assert RELAY_TOKEN_VARIABLE not in tool_free.capture.text_of("environment")
    assert tool_free.capture.homeless_argv == without_origin.capture.homeless_argv


# The endings only Grok's stream has, and the turn limit only its argv carries.


def a_relay_call_update(status: str) -> dict[str, str]:
    return {"type": "tool_call_update", "toolCallId": "call-1", "status": status}


def a_native_call(name: str) -> dict[str, str]:
    return {"type": "tool_call", "toolCallId": "call-native", "toolName": name, "status": "pending"}


@pytest.mark.parametrize(
    ("options", "failure_class"),
    [
        pytest.param(
            {"lines_after_calls": [a_native_call("read_file")]},
            "GrokToolCalled",
            id="native-tool-after-a-relay-call",
        ),
        pytest.param(
            {"lines_after_calls": [a_relay_call_update("failed")]},
            "GrokRelayCallRefused",
            id="relay-call-shown-as-failed",
        ),
        pytest.param(
            {"lines_after_calls": [{"type": "max_turns_reached"}]},
            "GrokTurnLimitReached",
            id="turn-limit",
        ),
        pytest.param({"exit_code": 1}, "GrokAnswerMissing", id="non-zero-exit-after-a-call"),
    ],
)
def test_a_grok_relay_run_ends_provider_error_on_what_its_stream_may_not_show(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    options: dict[str, Any],
    failure_class: str,
) -> None:
    run = a_tool_run(
        GROK_TABLE,
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[a_save_note()],
        **options,
    )

    assert (run.ending, len(run.app.calls), len(child_cleanup.listed())) == (
        PROVIDER_ERROR,
        1,
        1,
    )
    assert [kind for _event, kind, _function in failure_lines(caplog)] == [failure_class]
    eventually(lambda: relay_refuses(run.capture.relay_port, 10.0))
    assert (list(private_root_of(run.config).iterdir()), held_slots(GROK_TABLE.pool)) == ([], 0)


@pytest.mark.parametrize("max_tool_calls", [1, 4294967294])
def test_a_grok_relay_runs_turn_limit_is_one_turn_beyond_the_services_call_limit(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    max_tool_calls: int,
) -> None:
    run = a_tool_run(
        GROK_TABLE,
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[a_save_note()],
        runs={"max_tool_calls": max_tool_calls},
    )

    argv = run.capture.homeless_argv
    assert run.ending == (RunState.SUCCEEDED, None)
    assert argv[argv.index("--max-turns") + 1] == str(max_tool_calls + 1)


def test_grok_goes_on_after_the_relay_refuses_its_discovery_and_lists_the_tools(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = a_tool_run(GROK_TABLE, tmp_path, authority, child_cleanup, monkeypatch)

    replies = [run.capture.replies_to(method) for method in ("server/discover", "tools/list")]
    assert [[reply["status"] for reply in sent] for sent in replies] == [[400], [200]]
    assert run.ending == (RunState.SUCCEEDED, None)
