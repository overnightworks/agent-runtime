"""``GrokRun`` describes one fresh Grok run for the neutral run entry; its decoder fails closed.

The description rows read the child it would start; the decoder rows feed lines directly; the
end-to-end rows drive ``astream_cli_run`` against a ``/bin/sh`` stand-in for the CLI.
"""

from __future__ import annotations

import json
import logging
import math
import tomllib
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from tests.support.cli_run_support import (
    a_drive,  # noqa: F401 - the fixture is named in its decorator
    a_private_working_root,  # noqa: F401 - the fixture is named in its decorator
)
from tests.support.grok_support import COMMANDS, END, PRECONDITIONS_MARKER, USAGE, a_text, grok_line
from tests.support.grok_support import a_fake_grok as a_fake_grok_expecting
from tests.support.provider_test_support import a_grok_login

from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.cli_run import (
    RELAY_TOKEN_VARIABLE,
    CliStreamLineTooLong,
    RelayAccess,
    astream_cli_run,
)
from agent_runtime.cli_settings import CliSettings
from agent_runtime.config import PrivateWorkingRoot, reset_config
from agent_runtime.errors import CliLoginRefusedError
from agent_runtime.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_runtime.grok.run import (
    GROK_CLI_LOGIN_REFUSED_DETAIL,
    GROK_CLI_PROCESS_POOL,
    GrokAnswerMissing,
    GrokRelayCallRefused,
    GrokRun,
    GrokRunFailure,
    GrokStreamDecoder,
    GrokToolCalled,
    GrokTurnFailed,
    GrokTurnLimitReached,
    GrokUnknownStreamPart,
    GrokUnreadableLine,
)
from agent_runtime.grok_login import assess_grok_login
from agent_runtime.spawn import ChildProcessSpecificationError, closed_environment
from agent_runtime_client.wire import TurnMessage

BINARY = Path("/opt/grok/bin/grok")
SYSTEM_TEXT = "system-canary-7d2e"
HISTORY_TEXT = "history-canary-91ab"
CLI_TEXT = "cli-text-canary-5c30"
LOGIN_KEY = "grok-synthetic-key"
SETTING_VALUE = "setting-value-canary-4f1a"
RELAY_SECRET = "relay-secret-canary-0e5b"
EXPECTED_PROMPT = f"User: first question\n\nAssistant: first answer\n\nUser: {HISTORY_TEXT}"

BASE_ARGUMENTS = (
    "--output-format",
    "streaming-json",
    "--deny",
    "*",
    "--max-turns",
    "1",
    "--no-subagents",
    "--disable-web-search",
)
HAPPY_STREAM = (COMMANDS, a_text("first "), a_text("second"), USAGE, END)
NOT_SIGNED_IN = grok_line(type="error", message=f"Not signed in. {CLI_TEXT}")


def a_history() -> tuple[TurnMessage, ...]:
    return (
        TurnMessage(role="user", text="first question"),
        TurnMessage(role="assistant", text="first answer"),
        TurnMessage(role="user", text=HISTORY_TEXT),
    )


def a_login() -> bytes:
    return json.dumps(a_grok_login()).encode()


def a_run(**changes: Any) -> GrokRun:
    fields: dict[str, Any] = {
        "binary": BINARY,
        "login": assess_grok_login(a_login()),
        "system": SYSTEM_TEXT,
        "history": a_history(),
        "settings": (),
    }
    return GrokRun(**{**fields, **changes})


a_fake_grok = partial(
    a_fake_grok_expecting, system_text=SYSTEM_TEXT, expected_prompt=EXPECTED_PROMPT
)


def decoded(
    lines: tuple[bytes, ...], returncode: int | None = None, *, relay_attached: bool = False
) -> list[StreamEvent]:
    """The events of ``lines``, and the ending when the process is taken to have exited."""
    decoder = GrokStreamDecoder(relay_attached=relay_attached)
    events: list[StreamEvent] = [event for line in lines for event in decoder.events_of(line)]
    return events if returncode is None else [*events, decoder.ending(returncode)]


@pytest.mark.parametrize(
    ("model", "model_option"),
    [
        pytest.param(None, (), id="no-model"),
        pytest.param("listed-model", ("--model=listed-model",), id="named-model"),
    ],
)
def test_the_argv_is_the_measured_base_plus_the_prompt_file_and_the_model(
    tmp_path: Path, model: str | None, model_option: tuple[str, ...]
) -> None:
    home = tmp_path / "home"
    arguments = a_run(model=model).start(home, tmp_path / "work").child.arguments

    assert arguments == (
        "--prompt-file",
        str(home / "prompt.txt"),
        *BASE_ARGUMENTS,
        *model_option,
    )


def test_the_environment_adds_only_the_run_home_as_grok_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    environment = a_run().start(home, tmp_path / "work").child.environment

    assert (environment["GROK_HOME"], environment["HOME"]) == (str(home), str(home))
    assert set(environment) == {*closed_environment(home), "GROK_HOME"}


def test_the_run_declares_no_picture_folder_and_no_stdin(tmp_path: Path) -> None:
    start = a_run().start(tmp_path / "home", tmp_path / "work")

    assert (start.picture_folders, start.stdin) == ((), b"")


def test_no_login_system_or_history_text_is_visible_to_a_process_listing(tmp_path: Path) -> None:
    run = a_run()
    start = run.start(tmp_path / "home", tmp_path / "work")

    listing = " ".join(
        [*start.child.arguments, *start.child.environment.values(), repr(run), repr(start)]
    )
    assert not any(secret in listing for secret in (LOGIN_KEY, SYSTEM_TEXT, HISTORY_TEXT))


def test_the_home_gets_the_blanked_login_the_rules_and_the_prompt_as_files() -> None:
    login = assess_grok_login(a_login())
    files = a_run(login=login).run_files()

    assert files == {
        "auth.json": login.blanked_document,
        "AGENTS.md": SYSTEM_TEXT.encode(),
        "prompt.txt": EXPECTED_PROMPT.encode(),
        "config.toml": b"",
    }
    assert json.loads(files["auth.json"])["xai.example"]["refresh_token"] == ""


def test_the_levels_are_written_merged_to_the_config_file_and_reach_no_argv_or_environment(
    tmp_path: Path,
) -> None:
    settings = (
        {"invented": {"first": 1, "kept": SETTING_VALUE}, "scalar": {"was": "table"}},
        {"invented": {"first": 2}, "scalar": "now", "list": ["a"]},
        {"list": ["b"]},
    )
    run = a_run(settings=settings)
    child = run.start(tmp_path / "home", tmp_path / "work").child

    assert tomllib.loads(run.run_files()["config.toml"].decode()) == {
        "invented": {"first": 2, "kept": SETTING_VALUE},
        "scalar": "now",
        "list": ["b"],
    }
    leaks = " ".join([*child.arguments, *child.environment.values(), repr(run)])
    assert SETTING_VALUE not in leaks
    assert LOGIN_KEY not in run.run_files()["config.toml"].decode()


@pytest.mark.parametrize(
    ("changes", "field_name"),
    [
        pytest.param({"binary": Path("grok")}, "binary", id="relative-binary"),
        pytest.param({"history": ()}, "history", id="empty-history"),
        pytest.param({"login": assess_grok_login(b"not json")}, "login", id="unusable-login"),
        pytest.param({"login": a_login()}, "login", id="login-of-the-wrong-type"),
    ],
)
def test_a_run_with_a_refused_field_is_not_built_and_the_error_names_the_field(
    changes: dict[str, Any], field_name: str
) -> None:
    with pytest.raises(ChildProcessSpecificationError, match=field_name) as refused:
        a_run(**changes)

    assert [
        secret
        for secret in (SYSTEM_TEXT, HISTORY_TEXT)
        if secret in str(refused.value) or secret in str(refused.value.args)
    ] == []


def a_relay() -> RelayAccess:
    return RelayAccess(url="http://127.0.0.1:4711/mcp", secret=RELAY_SECRET)


def relay_fields(**changes: Any) -> dict[str, Any]:
    return {"relay": a_relay(), "tool_timeout_seconds": 900.0, "max_tool_calls": 30, **changes}


def a_relay_run(*settings: CliSettings, **changes: Any) -> GrokRun:
    return a_run(settings=settings, **relay_fields(**changes))


def written_settings(run: GrokRun) -> dict[str, Any]:
    return tomllib.loads(run.run_files()["config.toml"].decode())


@pytest.mark.parametrize(
    ("max_tool_calls", "model", "model_option"),
    [
        pytest.param(1, None, (), id="one-call"),
        pytest.param(30, "listed-model", ("--model=listed-model",), id="thirty-calls-named-model"),
        pytest.param(4294967294, None, (), id="the-largest-call-limit"),
    ],
)
def test_a_relay_runs_argv_is_the_pinned_hypothesis_with_one_turn_beyond_its_call_limit(
    tmp_path: Path, max_tool_calls: int, model: str | None, model_option: tuple[str, ...]
) -> None:
    home = tmp_path / "home"
    run = a_relay_run(max_tool_calls=max_tool_calls, model=model)
    arguments = run.start(home, tmp_path / "work").child.arguments

    assert arguments == (
        "--prompt-file",
        str(home / "prompt.txt"),
        "--output-format",
        "streaming-json",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--allow",
        "MCPTool(relay__*)",
        *("--deny", "Bash", "--deny", "Read", "--deny", "Edit", "--deny", "Write"),
        *("--deny", "Grep", "--deny", "Glob", "--deny", "WebFetch", "--deny", "WebSearch"),
        "--max-turns",
        str(max_tool_calls + 1),
        "--no-subagents",
        "--disable-web-search",
        *model_option,
    )
    assert "*" not in arguments


def test_the_allow_rule_opens_exactly_the_server_the_config_entry_names(tmp_path: Path) -> None:
    run = a_relay_run()
    arguments = run.start(tmp_path / "home", tmp_path / "work").child.arguments

    [server] = written_settings(run)["mcp_servers"]
    assert arguments[arguments.index("--allow") + 1] == f"MCPTool({server}__*)"


@pytest.mark.parametrize(
    ("seconds", "whole_seconds"), [(1.0, 1), (2.5, 3), (899.01, 900), (900.0, 900)]
)
def test_a_relay_run_writes_the_relay_entry_over_the_merged_levels_beside_their_keys(
    seconds: float, whole_seconds: int
) -> None:
    levels: tuple[CliSettings, ...] = (
        {"invented": {"first": 1, "kept": SETTING_VALUE}, "permission": {"allow": ["Read"]}},
        {"invented": {"first": 2}},
    )
    settings = written_settings(a_relay_run(*levels, tool_timeout_seconds=seconds))
    servers = settings.pop("mcp_servers")

    assert settings == {
        "invented": {"first": 2, "kept": SETTING_VALUE},
        "permission": {"allow": ["Read"]},
    }
    assert servers == {
        "relay": {
            "url": a_relay().url,
            "bearer_token_env_var": RELAY_TOKEN_VARIABLE,
            "tool_timeout_sec": whole_seconds,
        }
    }


@pytest.mark.parametrize(
    "level",
    [
        pytest.param({"mcp_servers": {"relay": {"url": "https://elsewhere.test/"}}}, id="url"),
        pytest.param(
            {"mcp_servers": {"relay": {"bearer_token_env_var": "OTHER_VARIABLE"}}},
            id="bearer-variable",
        ),
        pytest.param({"mcp_servers": {"relay": {"invented": 1}}}, id="invented-key"),
        pytest.param({"mcp_servers": {}}, id="empty-servers"),
        pytest.param({"mcp_servers": "not-a-table"}, id="scalar-servers"),
    ],
)
def test_the_relay_entry_replaces_whatever_a_level_set_for_it(level: CliSettings) -> None:
    assert (
        written_settings(a_relay_run(level))["mcp_servers"]
        == written_settings(a_relay_run())["mcp_servers"]
    )


OTHER_SERVER: CliSettings = {"url": f"https://{SETTING_VALUE}.test/mcp"}


@pytest.mark.parametrize(
    "levels",
    [
        pytest.param(({"mcp_servers": {"other": OTHER_SERVER}},), id="another-server"),
        pytest.param(
            ({"mcp_servers": {"relay": {}, "other": OTHER_SERVER}},), id="beside-a-relay-entry"
        ),
        pytest.param(
            ({"mcp_servers": {"other": OTHER_SERVER}}, {"mcp_servers": "not-a-table"}),
            id="in-a-lower-level",
        ),
        pytest.param(
            (
                {"permission": {"allow": ["MCPTool(other__*)"]}},
                {"mcp_servers": {"other": OTHER_SERVER}},
            ),
            id="with-a-compact-allow-rule",
        ),
        pytest.param(
            (
                {
                    "permission": {"rules": [{"tool": "MCPTool(other__*)", "action": "allow"}]},
                    "mcp_servers": {"other": OTHER_SERVER},
                },
            ),
            id="with-a-structured-allow-rule",
        ),
    ],
)
def test_a_relay_run_refuses_a_level_naming_another_mcp_server_by_its_path_alone(
    levels: tuple[CliSettings, ...],
) -> None:
    with pytest.raises(ChildProcessSpecificationError, match=r"mcp_servers\.other") as refused:
        a_relay_run(*levels)

    assert [
        secret
        for secret in (SETTING_VALUE, RELAY_SECRET, SYSTEM_TEXT, HISTORY_TEXT)
        if secret in str(refused.value)
    ] == []


def test_a_run_without_a_relay_writes_another_mcp_server_as_it_was_set() -> None:
    level: CliSettings = {"mcp_servers": {"other": OTHER_SERVER}}

    assert written_settings(a_run(settings=(level,))) == level


def test_the_relay_secret_is_only_in_the_variable_the_entry_names(tmp_path: Path) -> None:
    home = tmp_path / "home"
    run = a_relay_run()
    start = run.start(home, tmp_path / "work")
    variable = written_settings(run)["mcp_servers"]["relay"]["bearer_token_env_var"]

    environment = start.child.environment
    holders = [name for name, value in environment.items() if RELAY_SECRET in value]
    assert (variable, holders, environment[variable]) == (
        RELAY_TOKEN_VARIABLE,
        [variable],
        RELAY_SECRET,
    )
    assert set(environment) == {*closed_environment(home), "GROK_HOME", variable}
    visible = [*start.child.arguments, *run.run_files().values(), repr(run), repr(start)]
    assert [part for part in visible if RELAY_SECRET in str(part)] == []


@pytest.mark.parametrize(
    ("changes", "field_name"),
    [
        pytest.param({"relay": a_relay()}, "tool_timeout_seconds", id="relay-alone"),
        pytest.param({"tool_timeout_seconds": 900.0}, "relay", id="timeout-alone"),
        pytest.param({"max_tool_calls": 30}, "relay", id="call-limit-alone"),
        pytest.param(
            {"relay": a_relay(), "tool_timeout_seconds": 900.0},
            "max_tool_calls",
            id="without-call-limit",
        ),
        pytest.param(
            {"relay": a_relay(), "max_tool_calls": 30}, "tool_timeout_seconds", id="without-timeout"
        ),
        pytest.param(
            {"tool_timeout_seconds": 900.0, "max_tool_calls": 30}, "relay", id="without-relay"
        ),
        *(
            pytest.param(
                relay_fields(tool_timeout_seconds=seconds),
                "tool_timeout_seconds",
                id=f"timeout-{seconds}",
            )
            for seconds in (math.nan, math.inf, 0.0, -1.0)
        ),
        *(
            pytest.param(
                relay_fields(max_tool_calls=calls), "max_tool_calls", id=f"calls-{calls!r}"
            )
            for calls in (True, 1.0, "30", 0, -1, 4294967295)
        ),
    ],
)
def test_a_relay_its_timeout_and_its_call_limit_come_together_and_in_range(
    changes: dict[str, Any], field_name: str
) -> None:
    with pytest.raises(ChildProcessSpecificationError, match=field_name) as refused:
        a_run(**changes)

    assert [
        secret
        for secret in (RELAY_SECRET, SYSTEM_TEXT, HISTORY_TEXT)
        if secret in str(refused.value) or secret in str(refused.value.args)
    ] == []


def test_the_streamed_text_is_its_text_events_then_the_whole_answer_stripped() -> None:
    events = decoded(HAPPY_STREAM, 0)

    assert events == [
        AssistantTextEvent(text="first "),
        AssistantTextEvent(text="second"),
        FinalEvent(text="first second"),
    ]


@pytest.mark.parametrize(
    "silent",
    [
        pytest.param(grok_line(type="thought", data=CLI_TEXT), id="thought"),
        pytest.param(USAGE, id="usage"),
        pytest.param(COMMANDS, id="available-commands"),
    ],
)
def test_thought_usage_and_command_lists_never_reach_the_events(silent: bytes) -> None:
    assert decoded((silent, a_text("answer"), END), 0) == [
        AssistantTextEvent(text="answer"),
        FinalEvent(text="answer"),
    ]


def test_a_completed_turn_without_words_ends_with_empty_text_for_the_wire_to_decide() -> None:
    assert decoded((END,), 0)[-1] == FinalEvent(text="")


def a_tool_call(name: str, *, call_id: object = "call-1", **fields: Any) -> bytes:
    return grok_line(
        type="tool_call", toolCallId=call_id, toolName=name, status="pending", **fields
    )


def a_use_tool(tool_name: object) -> bytes:
    return a_tool_call("use_tool", rawInput={"tool_name": tool_name, "arguments": {"t": CLI_TEXT}})


def an_update(status: object, *, call_id: object = "call-1") -> bytes:
    return grok_line(type="tool_call_update", toolCallId=call_id, status=status, rawOutput=CLI_TEXT)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(a_use_tool("relay__save_note"), id="use-tool-naming-a-relay-tool"),
        pytest.param(a_tool_call("relay__save_note"), id="a-relay-tool-by-name"),
        pytest.param(a_tool_call("search_tool", rawInput={"query": CLI_TEXT}), id="search-tool"),
    ],
)
def test_a_relay_call_and_its_running_or_completed_updates_are_dropped(call: bytes) -> None:
    updates = tuple(an_update(status) for status in ("pending", "in_progress", "completed", None))

    assert decoded((call, *updates, a_text("answer"), END), 0, relay_attached=True) == [
        AssistantTextEvent(text="answer"),
        FinalEvent(text="answer"),
    ]


def test_a_refused_login_is_the_cores_error_with_its_fixed_detail() -> None:
    with pytest.raises(CliLoginRefusedError, match=GROK_CLI_LOGIN_REFUSED_DETAIL):
        decoded((NOT_SIGNED_IN,))


def test_every_failure_class_has_its_own_fixed_message() -> None:
    classes = GrokRunFailure.__subclasses__()

    assert len({failure.message for failure in classes}) == len(classes)
    assert all(str(failure()) == failure.message for failure in classes)


@pytest.fixture(name="pool")
def a_grok_pool() -> CliProcessPool:
    return GROK_CLI_PROCESS_POOL


async def collected(
    root: PrivateWorkingRoot, run: GrokRun, sink: list[StreamEvent] | None = None
) -> list[StreamEvent]:
    """The run's events, also appended to ``sink`` as they arrive so a failed run keeps them."""
    events = [] if sink is None else sink
    async for event in astream_cli_run(root, run, max_files_bytes=1024):
        events.append(event)
    return events


def test_a_run_after_reset_config_needs_no_installed_configuration(
    drive, tmp_path: Path, private_root: Path
) -> None:
    reset_config()
    run = a_run(binary=a_fake_grok(tmp_path, HAPPY_STREAM))

    events = drive(collected(PrivateWorkingRoot(private_root), run))

    assert [event.text for event in events] == ["first ", "second", "first second"]
    assert not list(private_root.iterdir())


def test_the_child_sees_the_private_blanked_login_and_no_secret_on_argv(
    drive, tmp_path: Path, private_root: Path
) -> None:
    capture = tmp_path / "capture"
    run = a_run(binary=a_fake_grok(tmp_path, HAPPY_STREAM, capture=capture))

    drive(collected(PrivateWorkingRoot(private_root), run))

    argv = (capture / "argv").read_text()
    assert "--prompt-file" in argv.splitlines()
    assert [text for text in (LOGIN_KEY, SYSTEM_TEXT, HISTORY_TEXT) if text in argv] == []
    assert json.loads((capture / "auth.json").read_bytes()) == json.loads(
        run.login.blanked_document
    )


def test_a_relay_run_hands_its_secret_only_to_the_childs_environment_and_answers(
    drive, tmp_path: Path, private_root: Path
) -> None:
    reset_config()
    capture = tmp_path / "capture"
    stream = (COMMANDS, a_use_tool("relay__save_note"), an_update("completed"), a_text("ok"), END)
    run = a_relay_run(binary=a_fake_grok(tmp_path, stream, capture=capture))

    events = drive(collected(PrivateWorkingRoot(private_root), run))

    assert events == [AssistantTextEvent(text="ok"), FinalEvent(text="ok")]
    environment = (capture / "environment").read_text().splitlines()
    assert [line for line in environment if RELAY_SECRET in line] == [
        f"{RELAY_TOKEN_VARIABLE}={RELAY_SECRET}"
    ]
    config = tomllib.loads((capture / "config.toml").read_text())
    assert config["mcp_servers"]["relay"]["bearer_token_env_var"] == RELAY_TOKEN_VARIABLE
    given = [(capture / name).read_text() for name in ("argv", "config.toml", "auth.json", "home")]
    assert [part for part in given if RELAY_SECRET in part] == []
    assert not list(private_root.iterdir())


def a_tool_event(kind: str) -> bytes:
    return grok_line(type=kind, rawInput=CLI_TEXT)


@pytest.mark.parametrize(
    ("stream", "then", "failure"),
    [
        pytest.param((NOT_SIGNED_IN,), "exit 1", CliLoginRefusedError, id="login-refused"),
        pytest.param((a_tool_event("tool_call"),), "exit 0", GrokToolCalled, id="tool-call"),
        pytest.param(
            (a_tool_event("tool_call_update"),), "exit 0", GrokToolCalled, id="tool-call-update"
        ),
        pytest.param(
            (a_text("answer"), a_tool_event("tool_call"), END),
            "exit 0",
            GrokToolCalled,
            id="tool-call-after-the-answer",
        ),
        pytest.param(
            (a_text("answer"), a_tool_event("tool_call_update"), END),
            "exit 0",
            GrokToolCalled,
            id="tool-call-update-after-the-answer",
        ),
        pytest.param(
            (a_use_tool("relay__save_note"),), "exit 0", GrokToolCalled, id="use-tool-naming-relay"
        ),
        pytest.param(
            (a_tool_call("relay__save_note"),), "exit 0", GrokToolCalled, id="relay-tool-by-name"
        ),
        pytest.param((a_tool_call("search_tool"),), "exit 0", GrokToolCalled, id="search-tool"),
        pytest.param(
            (grok_line(type="surprise", x=CLI_TEXT),),
            "exit 0",
            GrokUnknownStreamPart,
            id="unknown-type",
        ),
        pytest.param(
            (grok_line(type="max_turns_reached", x=CLI_TEXT),),
            "exit 1",
            GrokTurnLimitReached,
            id="turn-limit",
        ),
        pytest.param(
            (f"not json {CLI_TEXT}\n".encode(),), "exit 0", GrokUnreadableLine, id="not-json"
        ),
        pytest.param((b'["list"]\n',), "exit 0", GrokUnreadableLine, id="not-an-object"),
        pytest.param((grok_line(data=CLI_TEXT),), "exit 0", GrokUnreadableLine, id="no-type"),
        pytest.param(
            (grok_line(type="text", data=[CLI_TEXT]),),
            "exit 0",
            GrokUnreadableLine,
            id="text-not-a-string",
        ),
        pytest.param((grok_line(type="text"),), "exit 0", GrokUnreadableLine, id="text-no-data"),
        pytest.param(
            (grok_line(type="error", message=[CLI_TEXT]),),
            "exit 0",
            GrokUnreadableLine,
            id="error-message-not-a-string",
        ),
        pytest.param(
            (grok_line(type="error", message=f"quota {CLI_TEXT}"),),
            "exit 1",
            GrokTurnFailed,
            id="error",
        ),
        pytest.param((grok_line(type="error"),), "exit 1", GrokTurnFailed, id="error-no-message"),
        pytest.param((a_text("answer"), END), "exit 3", GrokAnswerMissing, id="bad-exit"),
        pytest.param((a_text("answer"),), "exit 0", GrokAnswerMissing, id="no-end"),
        pytest.param((), "exit 0", GrokAnswerMissing, id="empty-stream"),
    ],
)
def test_a_failed_run_raises_its_closed_class_without_cli_text_and_removes_the_tree(
    drive,
    tmp_path: Path,
    private_root: Path,
    caplog: pytest.LogCaptureFixture,
    stream: tuple[bytes, ...],
    then: str,
    failure: type[Exception],
) -> None:
    run = a_run(binary=a_fake_grok(tmp_path, stream, then))

    ends_closed(drive, tmp_path, private_root, caplog, run, failure)


@pytest.mark.parametrize(
    ("stream", "then", "failure"),
    [
        *(
            pytest.param((a_tool_call(name),), "exit 0", GrokToolCalled, id=name)
            for name in ("read_file", "run_terminal_cmd", "todo_write", "other__lookup")
        ),
        *(
            pytest.param((a_use_tool(name),), "exit 0", GrokToolCalled, id=f"use-tool-{name}")
            for name in ("other__lookup", "relay_save_note", "relay__", None, 7)
        ),
        pytest.param(
            (a_tool_call("use_tool", rawInput=CLI_TEXT),),
            "exit 0",
            GrokToolCalled,
            id="use-tool-without-an-object",
        ),
        pytest.param(
            (a_use_tool("relay__save_note"), an_update("completed", call_id="call-2")),
            "exit 0",
            GrokToolCalled,
            id="update-of-a-call-not-admitted",
        ),
        pytest.param(
            (an_update("completed", call_id=None),), "exit 0", GrokToolCalled, id="update-no-id"
        ),
        pytest.param(
            (a_text("answer"), a_tool_call("read_file"), END),
            "exit 0",
            GrokToolCalled,
            id="native-tool-after-the-answer",
        ),
        *(
            pytest.param(
                (a_use_tool("relay__save_note"), an_update(status), a_text("answer"), END),
                "exit 0",
                GrokRelayCallRefused,
                id=f"relay-call-{status}",
            )
            for status in ("failed", "refused", "surprise", 3)
        ),
        *(
            pytest.param(
                (grok_line(type="tool_call", status="pending", **fields),),
                "exit 0",
                GrokUnreadableLine,
                id=f"tool-call-{name}",
            )
            for name, fields in (
                ("without-id", {"toolName": "relay__save_note"}),
                ("id-not-a-string", {"toolCallId": 1, "toolName": "relay__save_note"}),
                ("without-name", {"toolCallId": "call-1"}),
                ("name-not-a-string", {"toolCallId": "call-1", "toolName": ["relay__x"]}),
            )
        ),
        pytest.param(
            (a_use_tool("relay__save_note"), grok_line(type="max_turns_reached", x=CLI_TEXT)),
            "exit 1",
            GrokTurnLimitReached,
            id="turn-limit",
        ),
        pytest.param(
            (a_use_tool("relay__save_note"), an_update("completed"), a_text("answer"), END),
            "exit 3",
            GrokAnswerMissing,
            id="bad-exit-after-relay-calls",
        ),
        pytest.param((NOT_SIGNED_IN,), "exit 1", CliLoginRefusedError, id="login-refused"),
    ],
)
def test_a_failed_relay_run_raises_its_closed_class_without_cli_text_and_removes_the_tree(
    drive,
    tmp_path: Path,
    private_root: Path,
    caplog: pytest.LogCaptureFixture,
    stream: tuple[bytes, ...],
    then: str,
    failure: type[Exception],
) -> None:
    run = a_relay_run(binary=a_fake_grok(tmp_path, stream, then))

    ends_closed(drive, tmp_path, private_root, caplog, run, failure)


def ends_closed(
    drive,
    tmp_path: Path,
    private_root: Path,
    caplog: pytest.LogCaptureFixture,
    run: GrokRun,
    failure: type[Exception],
) -> None:
    """Drive ``run`` and check it ended with ``failure``, no ending, no CLI text and no tree."""
    caplog.set_level(logging.DEBUG)
    seen: list[StreamEvent] = []
    scenario = collected(PrivateWorkingRoot(private_root), run, seen)

    with pytest.raises(failure) as ended:
        drive(scenario)

    assert (tmp_path / PRECONDITIONS_MARKER).exists()
    assert not any(isinstance(event, FinalEvent) for event in seen)
    assert (ended.value.__cause__, ended.value.__context__) == (None, None)
    assert CLI_TEXT not in f"{ended.value} {caplog.text}"
    assert not list(private_root.iterdir())


def test_a_line_over_the_limit_ends_the_run_and_the_tree_is_removed(
    drive, tmp_path: Path, private_root: Path
) -> None:
    oversized = (b"a" * (a_run().line_limit_bytes + 1) + b"\n",)
    run = a_run(binary=a_fake_grok(tmp_path, oversized))
    scenario = collected(PrivateWorkingRoot(private_root), run)

    with pytest.raises(CliStreamLineTooLong):
        drive(scenario)

    assert list(private_root.iterdir()) == []
