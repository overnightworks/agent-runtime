"""``ClaudeRun`` carries the operator's tools and settings; its decoder accepts exactly them.

The sandbox default is Claude's user layer in the run home and the operator's settings are the
higher ``--settings`` layer (decision record 0001, A1b measured: claude 2.1.302).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from tests.support.provider_test_support import (
    RELAY_CONFIG,
    RELAY_CONFIG_NAME,
    RELAY_SECRET,
    SYSTEM_PROMPT_NAME,
    SYSTEM_TEXT,
    TEXT_LINE,
    an_init,
)

from agent_runtime.claude.run import ClaudeRun, ClaudeStreamDecoder
from agent_runtime.cli_run import RelayAccess, RunSurfaceRefused, SurfaceVerdict
from agent_runtime.spawn import ChildProcessSpecificationError
from agent_runtime_client.wire import TurnMessage, TurnRole

TOKEN = "sk-ant-oat01-login-token-canary-5d02"
VALUE_CANARY = "setting-value-canary-91c4"
CLI_CANARY = "cli-announced-canary-77ae"
ACCESS = RelayAccess("http://127.0.0.1:41234/mcp", RELAY_SECRET)
RELAY_ENTRY = {"name": "relay", "status": "connected"}
SANDBOX_DEFAULT = {
    "sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}
}
USER_SETTINGS_NAME = "settings.json"
OPERATOR_SETTINGS_NAME = "cli-settings.json"
CONFIG_DIRECTORY_VARIABLE = "CLAUDE_CONFIG_DIR"
HOME = Path("/run/home")


def a_run(*levels: dict[str, Any], **overrides: object) -> ClaudeRun:
    fields: dict[str, object] = {
        "binary": Path("/opt/claude/claude"),
        "token": SecretStr(TOKEN),
        "system": SYSTEM_TEXT,
        "max_tool_calls": 30,
        "history": (TurnMessage(role=TurnRole.USER, text="question"),),
        "relay": None,
        "tool_timeout_seconds": 600.0,
        "model": None,
        "settings": levels,
    }
    return ClaudeRun(**{**fields, **overrides})  # type: ignore[arg-type]


def arguments_of(run: ClaudeRun) -> tuple[str, ...]:
    return run.start(HOME, Path("/run/work")).child.arguments


def environment_of(run: ClaudeRun) -> dict[str, str]:
    return dict(run.start(HOME, Path("/run/work")).child.environment)


def value_after(arguments: tuple[str, ...], option: str) -> str:
    return arguments[arguments.index(option) + 1]


def files_of(run: ClaudeRun) -> dict[str, Any]:
    files = run.run_files()
    return {name: json.loads(body) for name, body in files.items() if name != SYSTEM_PROMPT_NAME}


# The adapter's files, argv and environment.


@pytest.mark.parametrize(
    "levels", [(), ({}, {}, {})], ids=["no-levels", "three-empty-levels"]
)
def test_a_run_without_tools_or_settings_keeps_the_tool_free_run(
    levels: tuple[dict[str, Any], ...]
) -> None:
    run = a_run(*levels)
    arguments = arguments_of(run)

    assert (
        value_after(arguments, "--tools"),
        value_after(arguments, "--setting-sources"),
        "--settings" in arguments,
        "--allowedTools" in arguments,
        sorted(run.run_files()),
        CONFIG_DIRECTORY_VARIABLE in environment_of(run),
    ) == ("", "", False, False, [SYSTEM_PROMPT_NAME], False)


def test_the_sandbox_default_is_the_user_layer_and_the_operators_file_the_higher_one() -> None:
    off = {"settings": {"sandbox": {"enabled": False}}}
    run = a_run(off, {"tools": ["Bash"]}, {})
    arguments = arguments_of(run)

    assert (
        files_of(run),
        value_after(arguments, "--setting-sources"),
        value_after(arguments, "--settings"),
        environment_of(run)[CONFIG_DIRECTORY_VARIABLE],
    ) == (
        {
            USER_SETTINGS_NAME: SANDBOX_DEFAULT,
            OPERATOR_SETTINGS_NAME: {"sandbox": {"enabled": False}},
        },
        "user",
        str(HOME / OPERATOR_SETTINGS_NAME),
        str(HOME),
    )


def test_listed_tools_reach_tools_and_allowed_tools_and_only_the_default_file_is_written() -> None:
    run = a_run({"tools": ["Bash", "WebSearch"]})
    arguments = arguments_of(run)

    assert (
        value_after(arguments, "--tools"),
        arguments[-2:],
        value_after(arguments, "--setting-sources"),
        "--settings" in arguments,
        files_of(run),
        environment_of(run)[CONFIG_DIRECTORY_VARIABLE],
        {"--strict-mcp-config", "--disable-slash-commands"} <= set(arguments),
    ) == (
        "Bash,WebSearch",
        ("--allowedTools", "Bash,WebSearch"),
        "user",
        False,
        {USER_SETTINGS_NAME: SANDBOX_DEFAULT},
        str(HOME),
        True,
    )


def test_a_relay_beside_listed_tools_comes_first_in_the_last_option() -> None:
    run = a_run({"tools": ["Bash", "WebSearch"]}, relay=ACCESS)
    arguments = arguments_of(run)

    assert (arguments[-2:], arguments.count("--allowedTools")) == (
        ("--allowedTools", "mcp__relay__*,Bash,WebSearch"),
        1,
    )
    assert json.loads(run.run_files()[RELAY_CONFIG_NAME]) == RELAY_CONFIG


def test_a_level_without_tools_keeps_the_lower_list_and_an_empty_list_clears_it() -> None:
    kept = a_run({"tools": ["Bash"]}, {"settings": {"model": "x"}})
    cleared = a_run({"tools": ["Bash"]}, {"tools": []})

    assert (
        value_after(arguments_of(kept), "--tools"),
        value_after(arguments_of(cleared), "--tools"),
        sorted(cleared.run_files()),
    ) == ("Bash", "", [SYSTEM_PROMPT_NAME])


def test_settings_without_tools_reach_only_the_operators_file_and_the_sources_stay_empty() -> None:
    table = {"permissions": {"deny": ["Read"]}}
    run = a_run({"settings": table})
    arguments = arguments_of(run)

    assert (
        value_after(arguments, "--tools"),
        value_after(arguments, "--setting-sources"),
        value_after(arguments, "--settings"),
        CONFIG_DIRECTORY_VARIABLE in environment_of(run),
        files_of(run),
    ) == ("", "", str(HOME / OPERATOR_SETTINGS_NAME), False, {OPERATOR_SETTINGS_NAME: table})


def test_levels_merge_per_key_with_the_later_level_winning() -> None:
    run = a_run(
        {"settings": {"permissions": {"deny": ["Read"], "ask": ["Edit"]}, "outputStyle": "a"}},
        {"settings": {"permissions": {"deny": ["Write"]}}},
    )

    assert files_of(run) == {
        OPERATOR_SETTINGS_NAME: {
            "permissions": {"deny": ["Write"], "ask": ["Edit"]},
            "outputStyle": "a",
        }
    }


def test_a_setting_value_lives_only_in_the_operators_file_and_the_token_nowhere_written() -> None:
    run = a_run({"tools": ["Bash"], "settings": {"outputStyle": VALUE_CANARY}})
    carriers = {
        "argv": " ".join(arguments_of(run)),
        **{name: body.decode() for name, body in run.run_files().items()},
    }

    assert [name for name, text in carriers.items() if VALUE_CANARY in text] == [
        OPERATOR_SETTINGS_NAME
    ]
    assert [name for name, text in carriers.items() if TOKEN in text] == []


# What a core host can pass is refused where it is wrong, without echoing the value.

MALFORMED_LEVELS = [
    pytest.param({"tools": VALUE_CANARY}, id="tools-not-a-list"),
    pytest.param({"tools": ["Bash", 1]}, id="tool-not-a-string"),
    pytest.param({"tools": [""]}, id="empty-name"),
    pytest.param({"tools": [f"{VALUE_CANARY},Read"]}, id="comma"),
    pytest.param({"tools": [f"{VALUE_CANARY} Read"]}, id="space"),
    pytest.param({"tools": [f"{VALUE_CANARY}\tRead"]}, id="tab"),
    pytest.param({"tools": [f"-{VALUE_CANARY}"]}, id="leading-dash"),
    pytest.param({"settings": VALUE_CANARY}, id="settings-not-a-table"),
    pytest.param({VALUE_CANARY: 1}, id="another-key"),
]


@pytest.mark.parametrize("level", MALFORMED_LEVELS)
def test_a_malformed_level_is_refused_at_construction_without_its_value(
    level: dict[str, Any],
) -> None:
    with pytest.raises(ChildProcessSpecificationError) as refusal:
        a_run(level)

    assert VALUE_CANARY not in str(refusal.value)


UNENCODABLE = [
    pytest.param(date(2026, 10, 29), "date", id="date"),
    pytest.param(time(12, 0), "time", id="time"),
    pytest.param(datetime(2026, 10, 29, tzinfo=UTC), "datetime", id="datetime"),
    pytest.param(math.inf, "float", id="inf"),
    pytest.param(-math.inf, "float", id="negative-inf"),
    pytest.param(math.nan, "float", id="nan"),
]


@pytest.mark.parametrize(("value", "type_name"), UNENCODABLE)
def test_a_value_json_cannot_hold_raises_naming_its_path_and_type_beside_a_canary(
    value: object, type_name: str
) -> None:
    run = a_run({"settings": {"hooks": {"later": [value], "canary": VALUE_CANARY}}})

    with pytest.raises(TypeError) as refusal:
        run.run_files()

    assert str(refusal.value) == (
        f"Setting settings.hooks.later[0] is a {type_name}, which JSON cannot hold"
    )


# The start check: exactly the listed tools plus the relay's.


def refusal_of(decoder: ClaudeStreamDecoder, *lines: bytes) -> RunSurfaceRefused:
    with pytest.raises(RunSurfaceRefused) as refusal:
        for each in lines:
            decoder.events_of(each)
    return refusal.value


def a_decoder(listed: set[str], *, relay: bool = False) -> ClaudeStreamDecoder:
    return ClaudeStreamDecoder(relay_attached=relay, listed_tools=frozenset(listed))


@pytest.mark.parametrize(
    ("listed", "relay", "tools"),
    [
        pytest.param({"Bash", "WebSearch"}, False, ["Bash", "WebSearch"], id="listed"),
        pytest.param({"Bash"}, True, ["Bash", "mcp__relay__lookup"], id="listed-and-relay"),
        pytest.param(set(), True, ["mcp__relay__lookup"], id="relay-only"),
        pytest.param({"Bash"}, False, ["Bash", "Bash"], id="announced-twice"),
    ],
)
def test_an_announcement_of_exactly_the_listed_tools_and_the_relays_is_accepted(
    listed: set[str], relay: bool, tools: list[str]
) -> None:
    servers = [RELAY_ENTRY] if relay else []
    decoder = a_decoder(listed, relay=relay)

    assert decoder.events_of(an_init(tools=tools, mcp_servers=servers)) == []
    assert decoder.events_of(TEXT_LINE) != []


@pytest.mark.parametrize(
    ("listed", "relay", "tool"),
    [
        pytest.param({"Bash"}, False, "Read", id="unlisted-built-in"),
        pytest.param({"Bash"}, True, "mcp__notes__x", id="another-servers-tool"),
        pytest.param({"Bash"}, False, "mcp__relay__lookup", id="relay-tool-without-a-relay"),
        pytest.param(set(), True, "mcp__relayx__foo", id="server-named-like-the-relay"),
        pytest.param(set(), False, "Bash", id="nothing-listed"),
    ],
)
def test_an_announced_tool_that_is_neither_listed_nor_the_relays_refuses_the_run(
    listed: set[str], relay: bool, tool: str
) -> None:
    servers = [RELAY_ENTRY] if relay else []
    tools = sorted({*listed, tool})

    refusal = refusal_of(a_decoder(listed, relay=relay), an_init(tools=tools, mcp_servers=servers))

    assert refusal.verdict is SurfaceVerdict.UNLISTED_TOOL


def test_a_listed_name_the_announcement_lacks_refuses_the_run_and_the_log_names_it_from_the_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    decoder = a_decoder({"Bash", "InventedTool"})

    refusal = refusal_of(decoder, an_init(tools=["Bash"]))

    assert refusal.verdict is SurfaceVerdict.LISTED_TOOL_NOT_ANNOUNCED
    assert "A listed tool was not announced: InventedTool" in caplog.text


def test_an_unlisted_announced_name_never_reaches_a_message_or_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    decoder = a_decoder({"Bash", "InventedTool"})

    refusal = refusal_of(decoder, an_init(tools=["Bash", CLI_CANARY]))

    assert refusal.verdict is SurfaceVerdict.UNLISTED_TOOL
    assert [text for text in (str(refusal), repr(refusal), caplog.text) if CLI_CANARY in text] == []


def test_a_listed_name_the_announcement_lacks_is_named_once_in_sorted_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    refusal_of(a_decoder({"Write", "Bash", "Read"}), an_init(tools=["Read"]))

    assert [r.getMessage() for r in caplog.records if "listed tool" in r.getMessage()] == [
        "A listed tool was not announced: Bash, Write"
    ]
