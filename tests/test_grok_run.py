"""``GrokRun`` describes one fresh Grok run for the neutral run entry; its decoder fails closed.

The description rows read the child it would start; the decoder rows feed lines directly; the
end-to-end rows drive ``astream_cli_run`` against a ``/bin/sh`` stand-in for the CLI.
"""

from __future__ import annotations

import json
import logging
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
from agent_runtime.cli_run import CliStreamLineTooLong, astream_cli_run
from agent_runtime.config import PrivateWorkingRoot, reset_config
from agent_runtime.errors import CliLoginRefusedError
from agent_runtime.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_runtime.grok.run import (
    GROK_CLI_LOGIN_REFUSED_DETAIL,
    GROK_CLI_PROCESS_POOL,
    GrokAnswerMissing,
    GrokRun,
    GrokRunFailure,
    GrokStreamDecoder,
    GrokToolCalled,
    GrokTurnFailed,
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


def decoded(lines: tuple[bytes, ...], returncode: int | None = None) -> list[StreamEvent]:
    """The events of ``lines``, and the ending when the process is taken to have exited."""
    decoder = GrokStreamDecoder()
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


def test_a_setting_toml_cannot_hold_stops_the_run_naming_its_path_and_type_not_its_value() -> None:
    run = a_run(settings=({"nested": {"when": SETTING_VALUE.encode()}},))

    with pytest.raises(TypeError, match=r"nested\.when is a bytes") as refused:
        run.run_files()

    assert SETTING_VALUE not in str(refused.value)


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
            (grok_line(type="max_turns_reached", x=CLI_TEXT),),
            "exit 0",
            GrokUnknownStreamPart,
            id="unknown-type",
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
    caplog.set_level(logging.DEBUG)
    seen: list[StreamEvent] = []
    run = a_run(binary=a_fake_grok(tmp_path, stream, then))
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
