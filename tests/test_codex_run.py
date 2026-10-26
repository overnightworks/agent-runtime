"""``CodexRun`` describes one fresh Codex run for the neutral run entry; its decoder fails closed.

The description rows read the child it would start; the decoder rows feed lines directly; the
end-to-end rows drive ``astream_cli_run`` against a ``/bin/sh`` stand-in for the pinned CLI.
"""

from __future__ import annotations

import json
import logging
import tomllib
from collections.abc import Callable, Coroutine, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from anchored_leaders import spawn_warnings
from anchored_support import GONE, ChildCleanup
from codex_support import PRECONDITIONS_MARKER
from codex_support import a_fake_codex as a_fake_codex_expecting
from descendant_support import DescendantRow, Handshake, lingering_descendant
from provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    a_codex_login,
    a_private_root,
    drive_real_children,
    held_slots,
)

from agent_runtime.cli_run import CliStreamLineTooLong, astream_cli_run
from agent_runtime.codex.run import (
    CODEX_CLI_LOGIN_REFUSED_DETAIL,
    CodexAnswerMissing,
    CodexRun,
    CodexRunFailure,
    CodexStreamDecoder,
    CodexToolItem,
    CodexTurnFailed,
    CodexUnknownStreamPart,
    CodexUnreadableLine,
)
from agent_runtime.codex_login import assess_codex_login
from agent_runtime.config import PrivateWorkingRoot, reset_config
from agent_runtime.errors import CliLoginRefusedError
from agent_runtime.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_runtime.spawn import ChildProcessSpecificationError, closed_environment
from agent_runtime_client.wire import TurnMessage

BINARY = Path("/opt/codex/bin/codex")
SYSTEM_TEXT = "system-canary-7d2e"
HISTORY_TEXT = "history-canary-91ab"
CLI_TEXT = "cli-text-canary-5c30"
MODEL_OPTION = "--model=listed-model"
TOOL_ITEM_TYPES = (
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
    "collab_agent_tool_call",
    "image_generation",
    "image_gen",
)
ITEM_EVENT_TYPES = ("item.started", "item.updated", "item.completed")


def a_history() -> tuple[TurnMessage, ...]:
    return (
        TurnMessage(role="user", text="first question"),
        TurnMessage(role="assistant", text="first answer"),
        TurnMessage(role="user", text=HISTORY_TEXT),
    )


def a_run(**changes: Any) -> CodexRun:
    fields: dict[str, Any] = {
        "binary": BINARY,
        "login": assess_codex_login(a_codex_login()),
        "system": SYSTEM_TEXT,
        "history": a_history(),
        "model": None,
    }
    return CodexRun(**{**fields, **changes})


def line(**fields: Any) -> bytes:
    return (json.dumps(fields) + "\n").encode()


def an_item(event_type: str, item_type: str, **item: Any) -> bytes:
    return line(type=event_type, item={"id": "item_0", "type": item_type, **item})


def a_message(text: str) -> bytes:
    return an_item("item.completed", "agent_message", text=text)


THREAD = line(type="thread.started", thread_id="thread")
TURN = line(type="turn.started")
NOTICE = an_item("item.completed", "error", message=CLI_TEXT)
DONE = line(type="turn.completed", usage={})
REFUSED_TURN = line(
    type="turn.failed",
    error={
        "message": f"unexpected status 401 Unauthorized: {CLI_TEXT}, url: https://x.test/, "
        "request id: 401-abc"
    },
)
RECONNECTING = line(type="error", message=f"Reconnecting... 1/5 (401 Unauthorized {CLI_TEXT})")
HAPPY_STREAM = (THREAD, NOTICE, TURN, a_message("first"), a_message("second"), DONE)


EXPECTED_STDIN = f"User: first question\n\nAssistant: first answer\n\nUser: {HISTORY_TEXT}"
a_fake_codex = partial(
    a_fake_codex_expecting, system_text=SYSTEM_TEXT, expected_stdin=EXPECTED_STDIN
)


def decoded(lines: tuple[bytes, ...]) -> list[StreamEvent]:
    decoder = CodexStreamDecoder()
    return [event for each in lines for event in decoder.events_of(each)]


def decoded_and_ended(lines: tuple[bytes, ...], returncode: int = 0) -> list[StreamEvent]:
    decoder = CodexStreamDecoder()
    events = [event for each in lines for event in decoder.events_of(each)]
    return [*events, decoder.ending(returncode)]


BASE_ARGUMENTS = (
    "exec",
    "--json",
    "--sandbox",
    "read-only",
    "--skip-git-repo-check",
    "--ignore-user-config",
    "--ignore-rules",
    "--ephemeral",
    "--strict-config",
    "-c",
    'approval_policy="never"',
    "-c",
    "mcp_servers={}",
    "-c",
    "features.shell_tool=false",
    "-c",
    "features.unified_exec=false",
    "-c",
    "features.browser_use=false",
    "-c",
    "features.computer_use=false",
    "-c",
    "features.multi_agent=false",
    "-c",
    "features.image_generation=false",
    "-c",
    "features.plugins=false",
    "-c",
    "features.hooks=false",
    "-c",
    'web_search="disabled"',
    "-c",
    "features.code_mode_host=false",
    "-c",
    "features.code_mode=false",
    "-c",
    "features.code_mode_only=false",
)


@pytest.mark.parametrize(
    ("model", "model_option"),
    [
        pytest.param(None, (), id="no-model"),
        pytest.param("listed-model", (MODEL_OPTION,), id="named-model"),
    ],
)
def test_the_argv_is_the_measured_base_plus_the_system_file_and_the_model(
    tmp_path: Path, model: str | None, model_option: tuple[str, ...]
) -> None:
    start = a_run(model=model).start(tmp_path / "home", tmp_path / "work")

    arguments = start.child.arguments
    flag, system_switch, *tail = arguments[len(BASE_ARGUMENTS) :]
    assert (arguments[: len(BASE_ARGUMENTS)], flag, tuple(tail)) == (
        BASE_ARGUMENTS,
        "-c",
        (*model_option, "-"),
    )
    assert tomllib.loads(system_switch) == {
        "model_instructions_file": str(tmp_path / "home" / "system-prompt")
    }


@pytest.mark.parametrize(
    "name", ['we"ird', "back\\slash", 'both"\\', "grüße-😀", "line\nbreak", "del\x7fchar"]
)
def test_a_home_path_with_special_characters_stays_one_toml_string(
    tmp_path: Path, name: str
) -> None:
    home = tmp_path / name
    arguments = a_run().start(home, tmp_path / "work").child.arguments

    (switch,) = [each for each in arguments if each.startswith("model_instructions_file=")]
    assert tomllib.loads(switch) == {"model_instructions_file": str(home / "system-prompt")}


def test_the_environment_adds_only_the_run_home_as_codex_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    start = a_run().start(home, tmp_path / "work")

    environment = start.child.environment
    assert (environment["CODEX_HOME"], environment["HOME"]) == (str(home), str(home))
    assert set(environment) == {*closed_environment(home), "CODEX_HOME"}


def test_no_login_system_or_history_text_is_in_argv_environment_or_repr(tmp_path: Path) -> None:
    reset_config()
    run = a_run()
    start = run.start(tmp_path / "home", tmp_path / "work")

    visible = " ".join(
        [*start.child.arguments, *start.child.environment.values(), repr(run), repr(start)]
    )
    assert [
        secret
        for secret in (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN, SYSTEM_TEXT, HISTORY_TEXT)
        if secret in visible
    ] == []


def test_the_run_files_are_the_blanked_login_and_the_system_text() -> None:
    run = a_run()

    files = run.run_files()
    assert files["system-prompt"] == SYSTEM_TEXT.encode()
    assert files["auth.json"] == run.login.blanked_document
    assert json.loads(files["auth.json"])["tokens"]["refresh_token"] == ""
    assert sorted(files) == ["auth.json", "system-prompt"]


def test_the_stdin_is_the_history_as_one_transcript_in_app_order(tmp_path: Path) -> None:
    start = a_run().start(tmp_path / "home", tmp_path / "work")

    assert start.stdin == (
        f"User: first question\n\nAssistant: first answer\n\nUser: {HISTORY_TEXT}".encode()
    )


@pytest.mark.parametrize(
    ("changes", "field_name"),
    [
        pytest.param({"binary": Path("codex")}, "binary", id="relative-binary"),
        pytest.param({"history": ()}, "history", id="empty-history"),
        pytest.param(
            {"login": assess_codex_login(b"not json")}, "login", id="unusable-login"
        ),
        pytest.param({"login": a_codex_login()}, "login", id="login-of-the-wrong-type"),
    ],
)
def test_a_run_with_a_refused_field_is_not_built_and_the_error_names_the_field(
    changes: dict[str, Any], field_name: str
) -> None:
    with pytest.raises(ChildProcessSpecificationError, match=field_name) as refused:
        a_run(**changes)

    assert [secret for secret in (SYSTEM_TEXT, HISTORY_TEXT) if secret in str(refused.value)] == []


def test_the_happy_stream_is_its_text_events_then_the_last_message() -> None:
    assert decoded_and_ended(HAPPY_STREAM) == [
        AssistantTextEvent(text="first"),
        AssistantTextEvent(text="second"),
        FinalEvent(text="second"),
    ]


def test_a_reconnect_notice_before_the_turn_completes_still_succeeds() -> None:
    notice = line(type="error", message=f"Reconnecting... 1/5 {CLI_TEXT}")

    assert decoded_and_ended((THREAD, notice, TURN, a_message("answer"), DONE)) == [
        AssistantTextEvent(text="answer"),
        FinalEvent(text="answer"),
    ]


def test_reasoning_and_todo_text_never_reaches_the_events() -> None:
    quiet = (
        an_item("item.completed", "reasoning", text=CLI_TEXT),
        an_item("item.updated", "todo_list", text=CLI_TEXT),
        an_item("item.started", "agent_message", text=CLI_TEXT),
    )

    assert decoded((TURN, *quiet)) == []


def test_the_final_text_is_the_last_message_stripped() -> None:
    assert decoded_and_ended((a_message("  answer \n"), DONE))[-1] == FinalEvent(text="answer")


@pytest.mark.parametrize("event_type", ITEM_EVENT_TYPES)
@pytest.mark.parametrize("item_type", TOOL_ITEM_TYPES)
def test_a_tool_item_ends_the_run_at_its_first_event(event_type: str, item_type: str) -> None:
    stream = (an_item(event_type, item_type, command=CLI_TEXT),)

    with pytest.raises(CodexToolItem) as ended:
        decoded(stream)

    assert CLI_TEXT not in str(ended.value)


@pytest.mark.parametrize(
    ("failure", "stream"),
    [
        pytest.param(
            CodexUnknownStreamPart,
            (an_item("item.completed", "new_kind", text=CLI_TEXT),),
            id="unknown-item",
        ),
        pytest.param(
            CodexUnknownStreamPart, (line(type="turn.surprise", text=CLI_TEXT),), id="unknown-event"
        ),
        pytest.param(CodexUnreadableLine, (f"not json {CLI_TEXT}\n".encode(),), id="not-json"),
        pytest.param(CodexUnreadableLine, (b'["list"]\n',), id="not-an-object"),
        pytest.param(CodexUnreadableLine, (line(text=CLI_TEXT),), id="no-event-type"),
        pytest.param(CodexUnreadableLine, (line(type="item.completed"),), id="item-event-no-item"),
        pytest.param(
            CodexTurnFailed,
            (line(type="turn.failed", error={"message": f"401 {CLI_TEXT}"}),),
            id="failed-turn-with-a-bare-401",
        ),
        pytest.param(
            CodexTurnFailed,
            (line(type="turn.failed", error={"message": f"usage limit reached {CLI_TEXT}"}),),
            id="failed-turn-usage-limit",
        ),
        pytest.param(CodexTurnFailed, (line(type="turn.failed"),), id="failed-turn-no-error"),
        pytest.param(
            CodexUnreadableLine,
            (line(type="turn.failed", error=CLI_TEXT),),
            id="failed-turn-error-not-an-object",
        ),
        pytest.param(
            CodexUnreadableLine,
            (line(type="turn.failed", error={"message": [CLI_TEXT]}),),
            id="failed-turn-message-not-a-string",
        ),
        pytest.param(CliLoginRefusedError, (REFUSED_TURN,), id="failed-turn-login-refused"),
        pytest.param(
            CliLoginRefusedError,
            (RECONNECTING, RECONNECTING, REFUSED_TURN),
            id="failed-turn-login-refused-after-reconnecting-notices",
        ),
    ],
)
def test_a_stream_that_is_unknown_unreadable_or_failed_ends_closed_without_cli_text(
    failure: type[Exception], stream: tuple[bytes, ...]
) -> None:
    with pytest.raises(failure) as ended:
        decoded(stream)

    assert (CLI_TEXT in str(ended.value), ended.value.__cause__, ended.value.__context__) == (
        False,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("stream", "returncode"),
    [
        pytest.param((a_message("answer"), DONE), 1, id="nonzero-exit-after-completion"),
        pytest.param((TURN, a_message("answer")), 0, id="no-turn-completed"),
        pytest.param((TURN, DONE), 0, id="no-agent-message"),
        pytest.param((a_message("  \n"), DONE), 0, id="blank-agent-message"),
        pytest.param((), 0, id="empty-stream"),
    ],
)
def test_a_run_without_a_completed_answered_clean_turn_ends_with_no_answer(
    stream: tuple[bytes, ...], returncode: int
) -> None:
    with pytest.raises(CodexAnswerMissing):
        decoded_and_ended(stream, returncode)


def test_a_notice_of_a_refused_login_does_not_fail_a_turn_that_completes() -> None:
    events = decoded_and_ended((RECONNECTING, TURN, a_message("answer"), DONE))

    assert [type(event) for event in events] == [AssistantTextEvent, FinalEvent]


def test_a_refused_login_has_its_own_fixed_message_without_cli_text() -> None:
    with pytest.raises(CliLoginRefusedError) as refused:
        decoded((REFUSED_TURN,))

    assert (str(refused.value), refused.value.__cause__, refused.value.__context__) == (
        CODEX_CLI_LOGIN_REFUSED_DETAIL,
        None,
        None,
    )


def test_every_failure_class_has_its_own_fixed_message() -> None:
    failures = (
        CodexToolItem(),
        CodexUnknownStreamPart(),
        CodexUnreadableLine(),
        CodexTurnFailed(),
        CodexAnswerMissing(),
    )

    assert len({str(failure) for failure in failures}) == len(failures)
    assert all(isinstance(failure, CodexRunFailure) for failure in failures)


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
    yield lambda scenario: drive_real_children(child_cleanup, monkeypatch, scenario)
    assert [child_cleanup.state(owned) is GONE for owned in child_cleanup.listed()] == [
        True for _ in child_cleanup.listed()
    ]
    assert (held_slots(a_run().pool), thread_failures, spawn_warnings(caplog)) == (0, [], [])


async def collected(root: PrivateWorkingRoot, run: CodexRun) -> list[StreamEvent]:
    return [event async for event in astream_cli_run(root, run)]


def test_a_run_through_the_neutral_entry_streams_in_order_and_leaves_nothing_behind(
    drive, tmp_path: Path, private_root: Path
) -> None:
    reset_config()
    run = a_run(binary=a_fake_codex(tmp_path, HAPPY_STREAM))

    assert drive(collected(PrivateWorkingRoot(private_root), run)) == [
        AssistantTextEvent(text="first"),
        AssistantTextEvent(text="second"),
        FinalEvent(text="second"),
    ]
    assert list(private_root.iterdir()) == []


def test_the_group_is_gone_and_the_tree_removed_before_the_final_event(
    drive,
    tmp_path: Path,
    private_root: Path,
    handshake: Handshake,
    child_cleanup: ChildCleanup,
) -> None:
    lingering = lingering_descendant(
        "", exit_code=0, blocks=False, escapes=False, handshake=handshake
    )
    run = a_run(binary=a_fake_codex(tmp_path, HAPPY_STREAM, lingering))
    at_the_final_event: list[tuple[bool, int, list[Path]]] = []

    async def scenario() -> None:
        async for event in astream_cli_run(PrivateWorkingRoot(private_root), run):
            if isinstance(event, FinalEvent):
                at_the_final_event.append(
                    (row.descendant_is_dead(), held_slots(run.pool), list(private_root.iterdir()))
                )

    with DescendantRow(handshake, child_cleanup) as row:
        row.run_beside(lambda: drive(scenario()))

    assert at_the_final_event == [(True, 0, [])]


@pytest.mark.parametrize(
    ("stream", "then", "failure"),
    [
        pytest.param(
            (an_item("item.started", "command_execution", command=CLI_TEXT),),
            "exit 0",
            CodexToolItem,
            id="tool-item",
        ),
        pytest.param(
            (line(type="turn.failed", error={"message": f"401 {CLI_TEXT}"}),),
            "exit 1",
            CodexTurnFailed,
            id="failed-turn-with-a-bare-401",
        ),
        pytest.param(
            (RECONNECTING, REFUSED_TURN), "exit 1", CliLoginRefusedError, id="login-refused"
        ),
        pytest.param(
            (NOTICE, a_message("answer"), DONE), "exit 3", CodexAnswerMissing, id="bad-exit"
        ),
        pytest.param(
            (NOTICE, a_message("answer")), "exit 0", CodexAnswerMissing, id="not-completed"
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
    run = a_run(binary=a_fake_codex(tmp_path, stream, then))
    caplog.set_level(logging.DEBUG)
    scenario = collected(PrivateWorkingRoot(private_root), run)

    with pytest.raises(failure) as ended:
        drive(scenario)

    assert (tmp_path / PRECONDITIONS_MARKER).exists()
    assert CLI_TEXT not in f"{ended.value} {caplog.text}"
    assert list(private_root.iterdir()) == []


def test_a_line_over_the_limit_ends_the_run_and_the_tree_is_removed(
    drive, tmp_path: Path, private_root: Path
) -> None:
    oversized = (b"a" * (run_limit() + 1) + b"\n",)
    run = a_run(binary=a_fake_codex(tmp_path, oversized))
    scenario = collected(PrivateWorkingRoot(private_root), run)

    with pytest.raises(CliStreamLineTooLong):
        drive(scenario)

    assert list(private_root.iterdir()) == []


def run_limit() -> int:
    return a_run().line_limit_bytes
