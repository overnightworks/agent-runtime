"""``GrokRun`` describes one fresh Grok run for the neutral run entry; its decoder fails closed.

The description rows read the child it would start; the decoder rows feed lines directly; the
end-to-end rows drive ``astream_cli_run`` against a ``/bin/sh`` stand-in for the CLI.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from tests.support.anchored_leaders import spawn_warnings
from tests.support.anchored_support import GONE, ChildCleanup
from tests.support.grok_support import COMMANDS, END, PRECONDITIONS_MARKER, USAGE, a_text, grok_line
from tests.support.grok_support import a_fake_grok as a_fake_grok_expecting
from tests.support.provider_test_support import (
    a_grok_login,
    a_private_root,
    drive_real_children,
    held_slots,
)

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
    }
    assert json.loads(files["auth.json"])["xai.example"]["refresh_token"] == ""


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

    assert SYSTEM_TEXT not in str(refused.value) + str(refused.value.args)


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


@pytest.mark.parametrize(
    ("failure", "stream"),
    [
        pytest.param(GrokToolCalled, (grok_line(type="tool_call", rawInput=CLI_TEXT),), id="call"),
        pytest.param(
            GrokToolCalled, (grok_line(type="tool_call_update", status=CLI_TEXT),), id="update"
        ),
        pytest.param(
            GrokToolCalled,
            (a_text("answer"), grok_line(type="tool_call", rawInput=CLI_TEXT), END),
            id="call-after-the-answer",
        ),
        pytest.param(
            GrokUnknownStreamPart, (grok_line(type="max_turns_reached", x=CLI_TEXT),), id="unknown"
        ),
        pytest.param(GrokUnreadableLine, (f"not json {CLI_TEXT}\n".encode(),), id="not-json"),
        pytest.param(GrokUnreadableLine, (b'["list"]\n',), id="not-an-object"),
        pytest.param(GrokUnreadableLine, (grok_line(data=CLI_TEXT),), id="no-event-type"),
        pytest.param(
            GrokUnreadableLine, (grok_line(type="text", data=[CLI_TEXT]),), id="text-not-a-string"
        ),
        pytest.param(GrokUnreadableLine, (grok_line(type="text"),), id="text-without-data"),
        pytest.param(
            GrokTurnFailed, (grok_line(type="error", message=f"quota {CLI_TEXT}"),), id="error"
        ),
        pytest.param(GrokTurnFailed, (grok_line(type="error"),), id="error-without-message"),
        pytest.param(
            GrokUnreadableLine,
            (grok_line(type="error", message=[CLI_TEXT]),),
            id="error-message-not-a-string",
        ),
        pytest.param(CliLoginRefusedError, (NOT_SIGNED_IN,), id="not-signed-in"),
    ],
)
def test_a_stream_that_calls_a_tool_fails_or_is_unknown_ends_closed_without_cli_text(
    failure: type[Exception], stream: tuple[bytes, ...]
) -> None:
    with pytest.raises(failure) as ended:
        decoded(stream)

    chain = (ended.value.__cause__, ended.value.__context__)
    assert CLI_TEXT not in str(ended.value)
    assert chain == (None, None)


@pytest.mark.parametrize(
    ("stream", "returncode"),
    [
        pytest.param((a_text("answer"), END), 1, id="nonzero-exit-after-end"),
        pytest.param((a_text("answer"),), 0, id="no-end"),
        pytest.param((), 0, id="empty-stream"),
    ],
)
def test_a_run_without_an_end_and_a_clean_exit_ends_with_no_answer(
    stream: tuple[bytes, ...], returncode: int
) -> None:
    with pytest.raises(GrokAnswerMissing):
        decoded(stream, returncode)


def test_a_completed_turn_without_words_ends_with_empty_text_for_the_wire_to_decide() -> None:
    assert decoded((END,), 0)[-1] == FinalEvent(text="")


def test_a_refused_login_is_the_cores_error_with_its_fixed_detail() -> None:
    with pytest.raises(CliLoginRefusedError, match=GROK_CLI_LOGIN_REFUSED_DETAIL):
        decoded((NOT_SIGNED_IN,))


def test_every_failure_class_has_its_own_fixed_message() -> None:
    classes = GrokRunFailure.__subclasses__()

    assert len({failure.message for failure in classes}) == len(classes) == 5
    assert all(str(failure()) == failure.message for failure in classes)


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    return a_private_root(tmp_path)


@pytest.fixture
def drive(
    child_cleanup: ChildCleanup,
    thread_failures: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[Callable[[Coroutine[Any, Any, Any]], Any]]:
    yield partial(drive_real_children, child_cleanup, monkeypatch)
    assert all(child_cleanup.state(owned) is GONE for owned in child_cleanup.listed())
    assert held_slots(GROK_CLI_PROCESS_POOL) == 0
    assert thread_failures == [] == spawn_warnings(caplog)


async def collected(root: PrivateWorkingRoot, run: GrokRun) -> list[StreamEvent]:
    return [event async for event in astream_cli_run(root, run, max_files_bytes=1024)]


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


@pytest.mark.parametrize(
    ("stream", "then", "failure"),
    [
        pytest.param((NOT_SIGNED_IN,), "exit 1", CliLoginRefusedError, id="login-refused"),
        pytest.param(
            (a_text("answer"), grok_line(type="tool_call"), END),
            "exit 0",
            GrokToolCalled,
            id="tool-call",
        ),
        pytest.param((a_text("answer"), END), "exit 3", GrokAnswerMissing, id="bad-exit"),
        pytest.param((a_text("answer"),), "exit 0", GrokAnswerMissing, id="no-end"),
        pytest.param(
            (grok_line(type="error", message=CLI_TEXT),), "exit 1", GrokTurnFailed, id="error"
        ),
    ],
)
def test_a_failed_run_raises_its_closed_class_and_still_reaps_and_removes_the_tree(
    drive,
    tmp_path: Path,
    private_root: Path,
    caplog: pytest.LogCaptureFixture,
    stream: tuple[bytes, ...],
    then: str,
    failure: type[Exception],
) -> None:
    caplog.set_level(logging.DEBUG)
    run = a_run(binary=a_fake_grok(tmp_path, stream, then))

    with pytest.raises(failure) as ended:
        drive(collected(PrivateWorkingRoot(private_root), run))

    assert (tmp_path / PRECONDITIONS_MARKER).exists()
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
