"""The service's CLI runner answers a tool-free run through the real core and ends it correctly.

HTTP rows serve the real app on a loopback socket and run a fake Claude program as a real
child; no installed core configuration exists in any of them. The tool path has its own module
(``test_service_cli_runner_tools``). A tool-free run opens no connection to an app, so these
rows give the runner no TLS context.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi import FastAPI
from tests.support.anchored_support import ChildCleanup
from tests.support.bounds import TEST_WAIT_SECONDS
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.mcp_app_fake import TestAuthority
from tests.support.provider_test_support import (
    INIT_LINE,
    SUCCESS_LINE,
    TEXT_LINE,
    a_child_that_outlives_sigkill,
    a_claude_cli_child,
    a_claude_stdin_line,
    a_fake_claude,
    an_init,
    drive_real_children,
    drive_scripted,
    held_claude_slots,
    line,
)
from tests.support.service_cli_runner_support import (
    CANARY,
    FIRST_LOGIN,
    LOGIN_FILES_GONE_BAD,
    PLAIN_APP,
    SECOND_LOGIN,
    THREE_MESSAGE_TURN,
    VERSION_LINE,
    HeldReap,
    a_config,
    a_login_file,
    a_scripted_run,
    all_children_gone,
    assert_only_the_class_was_logged,
    drive_over_http,
    eventually,
    headers,
    held_reap,  # noqa: F401 - a fixture, requested by name
    leave_after,
    login_the_child_saw,
    post_a_run,
    private_root_of,
    shown,
    status_of,
    swap_login,
    through_the_service_entry,
    tool_free_runners,
    trusting,
)
from tests.support.service_support import (
    LISTED_MODEL,
    PROCESS_TOKEN,
    ClientSocket,
    Drive,
    StepClock,
    SteppedLoop,
    alice_admitted_run,
    decoded_events,
    driven,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    get_run,
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    post_run,
    process_token_file,
    record_signed_tokens,
    respond_through,
    run_body,
    the_app_on,
)

from agent_runtime.events import AssistantTextEvent
from agent_runtime.procfs import GroupCensus
from agent_runtime.spawn import KillUntilGoneResult
from agent_runtime_client.sse import RunStreamDecoder
from agent_runtime_client.wire import (
    ClientLoss,
    FailureCode,
    RunEvent,
    RunRequest,
    RunState,
    RunStatus,
)
from agent_runtime_service import cli_runner
from agent_runtime_service.config import Provider, ServiceConfig
from agent_runtime_service.run_events import (
    INVALID_EVENT_MESSAGE,
    RUNNER_FAILED_MESSAGE,
)


def test_a_tool_free_run_streams_the_cli_answer_and_succeeds(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path))

    def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (events[0].type, shown(events), status.state) == (
        "run_started",
        [("assistant_text", "hi"), ("succeeded", "done")],
        RunState.SUCCEEDED,
    )


def captured_carriers(capture: Path) -> dict[str, str]:
    files = {
        str(file.relative_to(capture)): file.read_text()
        for file in capture.rglob("*")
        if file.is_file() and file.parent != capture
    }
    return {
        "argv": (capture / "argv").read_text(),
        "stdin": (capture / "stdin").read_text(),
        "environment": (capture / "environment").read_text(),
        **files,
    }


def test_the_cli_receives_the_turns_system_history_token_and_bound(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    binary = a_fake_claude(tmp_path, token=PROCESS_TOKEN, max_turns=8, capture=capture)
    config = a_config(tmp_path, binary, runs={"max_tool_calls": 7})

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    carriers = captured_carriers(capture)
    assert (shown(events)[-1], carriers["environment"].count(PROCESS_TOKEN)) == (
        ("succeeded", "done"),
        1,
    )
    assert [
        name for name, text in carriers.items() if name != "environment" and PROCESS_TOKEN in text
    ] == []


def model_options_of(argv: str) -> list[str]:
    return [a for a in argv.splitlines() if a == "--model" or a.startswith("--model=")]


def test_the_cli_run_gets_the_configured_model_as_one_argument(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    config = a_config(tmp_path, a_fake_claude(tmp_path, capture=capture))

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    assert (shown(events)[-1], model_options_of((capture / "argv").read_text())) == (
        ("succeeded", "done"),
        [f"--model={LISTED_MODEL}"],
    )


def test_the_run_adapter_passes_the_routes_model(tmp_path: Path) -> None:
    turn = RunRequest.model_validate(run_body()).turn
    launch = cli_runner.ProviderLaunch(
        Provider.ANTHROPIC, tmp_path / "claude", process_token_file(), 7, 600.0
    )

    build = cli_runner.RUN_ADAPTERS[Provider.ANTHROPIC](launch)
    adapter = build(cli_runner.RunInputs(turn, LISTED_MODEL, None))

    arguments = adapter.start(tmp_path / "home", tmp_path / "work").child.arguments
    assert [a for a in arguments if a.startswith("--model")] == [f"--model={LISTED_MODEL}"]


def test_each_run_leaves_no_directory_slot_or_place_behind(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path), runs={"max_parallel_cli_runs": 1})

    def scenario(base_url: str) -> list[tuple[str | None, list[Path], int, list[bool]]]:
        afterwards = []
        for _ in range(2):
            events = post_a_run(base_url)
            afterwards.append(
                (
                    shown(events)[-1][0],
                    list(private_root_of(config).iterdir()),
                    held_claude_slots(),
                    all_children_gone(child_cleanup),
                )
            )
        return afterwards

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == [
        ("succeeded", [], 0, [True]),
        ("succeeded", [], 0, [True, True]),
    ]


def test_a_capability_never_reaches_the_tool_free_cli(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    signed = record_signed_tokens(monkeypatch)
    config = a_config(tmp_path, a_fake_claude(tmp_path, capture=capture), songmaker={})

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    carriers = captured_carriers(capture)
    assert (bool(signed), shown(events)[-1][0]) == (True, "succeeded")
    assert [name for name, text in carriers.items() if any(token in text for token in signed)] == []


def an_assistant_line(text: str) -> bytes:
    return line({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


LOGIN_REFUSED_LINE = line(
    {
        "type": "assistant",
        "is_api_error_message": True,
        "error": "authentication_failed",
        "message": {"content": [{"type": "text", "text": f"Not logged in {CANARY}"}]},
    }
)
TURN_LIMIT_LINE = line(
    {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": CANARY}
)
FOREIGN_INIT = an_init(apiKeySource="ANTHROPIC_API_KEY")


@pytest.mark.parametrize(
    ("stream", "then", "class_name", "taken", "verdict"),
    [
        pytest.param(
            (INIT_LINE, LOGIN_REFUSED_LINE), "exit 0", "CliLoginRefusedError", [], None, id="login"
        ),
        pytest.param(
            (INIT_LINE, TEXT_LINE, SUCCESS_LINE),
            f"echo {CANARY} >&2; echo {CANARY}; exit 1",
            "UnavailableError",
            [("assistant_text", "hi")],
            None,
            id="exit",
        ),
        pytest.param(
            (INIT_LINE, TEXT_LINE),
            "exit 0",
            "UnavailableError",
            [("assistant_text", "hi")],
            None,
            id="missing-result",
        ),
        pytest.param(
            (FOREIGN_INIT, TEXT_LINE),
            "exit 0",
            "RunSurfaceRefused",
            [],
            "FOREIGN_CREDENTIAL",
            id="surface",
        ),
        pytest.param(
            (INIT_LINE, TURN_LIMIT_LINE), "exit 0", "UnavailableError", [], None, id="turn-limit"
        ),
    ],
)
def test_a_failing_cli_ends_with_the_services_text_and_its_closed_cause(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stream: tuple[bytes, ...],
    then: str,
    class_name: str,
    taken: list[tuple[str, str]],
    verdict: str | None,
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path, stream=stream, then=then))

    def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert shown(events) == [*taken, ("failed", RUNNER_FAILED_MESSAGE)]
    assert [(event, kind) for event, kind, _ in failure_lines(caplog)] == [
        (f"The runner of run {events[0].run_id} failed", class_name)
    ]
    assert (
        verdict is None or verdict in caplog.text,
        CANARY in caplog.text,
        status.failure_code,
    ) == (
        True,
        False,
        FailureCode.PROVIDER_ERROR,
    )


LONE_SURROGATE_LINE = an_assistant_line("x\ud800 " + CANARY)
SURROGATE_RESULT_LINE = line(
    {"type": "result", "subtype": "success", "is_error": False, "result": "x\ud800 " + CANARY}
)


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param((INIT_LINE, LONE_SURROGATE_LINE, SUCCESS_LINE), id="assistant-text"),
        pytest.param((INIT_LINE, SURROGATE_RESULT_LINE), id="result"),
    ],
)
def test_a_lone_surrogate_from_the_cli_fails_closed_without_its_text(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stream: tuple[bytes, ...],
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path, stream=stream))

    def scenario(base_url: str) -> tuple[bytes, list[RunEvent]]:
        response = post_run(base_url, headers(), run_body(turn=THREE_MESSAGE_TURN))
        return response.content, decoded_events(response.content)

    raw, events = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert shown(events) == [("failed", INVALID_EVENT_MESSAGE)]
    assert (CANARY.encode() in raw, CANARY in caplog.text) == (False, False)


def test_a_run_whose_directories_cannot_be_made_fails_after_it_started(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path))
    private_root_of(config).chmod(0o500)

    try:
        events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)
    finally:
        private_root_of(config).chmod(0o700)

    assert (events[0].type, shown(events), child_cleanup.listed()) == (
        "run_started",
        [("failed", RUNNER_FAILED_MESSAGE)],
        (),
    )
    assert [kind for _, kind, _ in failure_lines(caplog)] == ["CliEnvironmentUnavailableError"]


def test_a_login_file_swapped_between_two_runs_serves_the_next_run_without_a_restart(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    login_file = a_login_file(tmp_path, FIRST_LOGIN)
    config = a_config(tmp_path, a_fake_claude(tmp_path, capture=capture), token_file=login_file)

    def scenario(base_url: str) -> list[tuple[str | None, str]]:
        first = post_a_run(base_url)
        seen = [(shown(first)[-1][0], login_the_child_saw(capture))]
        swap_login(login_file, SECOND_LOGIN)
        second = post_a_run(base_url)
        return [*seen, (shown(second)[-1][0], login_the_child_saw(capture))]

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == [
        ("succeeded", FIRST_LOGIN),
        ("succeeded", SECOND_LOGIN),
    ]


@pytest.mark.parametrize("spoil", LOGIN_FILES_GONE_BAD)
def test_a_login_file_gone_bad_fails_its_run_before_a_child_and_the_repaired_file_serves_the_next(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    spoil: Callable[[Path], None],
) -> None:
    login_file = a_login_file(tmp_path, FIRST_LOGIN)
    binary = a_fake_claude(tmp_path, token=SECOND_LOGIN)
    config = a_config(tmp_path, binary, runs={"max_parallel_cli_runs": 1}, token_file=login_file)
    spoil(login_file)

    def scenario(base_url: str) -> tuple[list[RunEvent], tuple[object, ...], list[RunEvent]]:
        failed = post_a_run(base_url)
        left_behind = (
            status_of(base_url, failed).failure_code,
            child_cleanup.listed(),
            held_claude_slots(),
            list(private_root_of(config).iterdir()),
        )
        swap_login(login_file, SECOND_LOGIN)
        return failed, left_behind, post_a_run(base_url)

    failed, left_behind, repaired = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(failed), left_behind, shown(repaired)[-1]) == (
        [("failed", RUNNER_FAILED_MESSAGE)],
        (FailureCode.PROVIDER_ERROR, (), 0, []),
        ("succeeded", "done"),
    )
    assert_only_the_class_was_logged(caplog, failed, tmp_path)


def read_status(base_url: str, run_id: uuid.UUID) -> RunStatus:
    response = get_run(base_url, headers(), str(run_id))
    return RunStatus.model_validate_json(response.content)


PARKED_AFTER_ONE_TEXT = (INIT_LINE, TEXT_LINE)
PARKED_AFTER_THREE_TEXTS = (
    INIT_LINE,
    TEXT_LINE,
    an_assistant_line("two"),
    an_assistant_line("three"),
)
PARKED = "exec sleep 600"


def a_parked_claude(
    directory: Path,
    *,
    for_the_client_socket: bool = False,
    says_again_after: Path | None = None,
    stream: tuple[bytes, ...] = PARKED_AFTER_ONE_TEXT,
) -> Path:
    """A fake that streams its texts and parks, saying one more once ``says_again_after`` is fed.

    The socket client asks the default one-line turn; the HTTP rows ask the three-message one.
    """
    then = PARKED
    if says_again_after is not None:
        again = an_assistant_line("again").decode()
        then = f"read -r go < {says_again_after}\ncat <<'EOF'\n{again}EOF\n{PARKED}"
    if for_the_client_socket:
        return a_fake_claude(
            directory,
            stream=stream,
            then=then,
            system_text="You help write songs.",
            stdin_line=a_claude_stdin_line("User: Hi"),
        )
    return a_fake_claude(directory, stream=stream, then=then)


def test_a_client_that_leaves_ends_the_cli_before_the_place_is_free(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,  # noqa: F811
) -> None:
    binary = a_parked_claude(tmp_path)
    config = a_config(tmp_path, binary, runs={"max_parallel_cli_runs": 1})

    def scenario(base_url: str) -> tuple[Any, ...]:
        run_id = leave_after(base_url, "assistant_text")
        held_reap.wait_until_reached()
        while_held = (
            read_status(base_url, run_id).state,
            post_run(base_url, headers(), run_body(turn=THREE_MESSAGE_TURN)).status_code,
            all_children_gone(child_cleanup),
        )
        held_reap.let_go.set()
        eventually(lambda: read_status(base_url, run_id).state is not RunState.RUNNING)
        status = read_status(base_url, run_id)
        afterwards = (
            status.state,
            status.client_lost,
            all_children_gone(child_cleanup),
            list(private_root_of(config).iterdir()),
            held_claude_slots(),
            held_reap.reaps,
        )
        next_run = leave_after(base_url, "assistant_text")
        eventually(lambda: read_status(base_url, next_run).state is not RunState.RUNNING)
        return while_held, afterwards, all_children_gone(child_cleanup)

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == (
        (RunState.RUNNING, 429, [False]),
        (RunState.CANCELLED, ClientLoss.DISCONNECTED, [True], [], 0, 1),
        [True, True],
    )


def in_stepped_loop(
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Coroutine[Any, Any, None],
) -> None:
    drive_real_children(child_cleanup, monkeypatch, scenario, loop_factory=SteppedLoop)


async def settled_and_held(held_reap: HeldReap) -> None:  # noqa: F811
    await asyncio.to_thread(held_reap.wait_until_reached)


def never_stopped_state(drive: Drive, client: ClientSocket) -> RunState:
    status = drive.status(client.run_id)
    assert status is not None
    return status.state


class StoppedRun(NamedTuple):
    status: RunStatus
    afterwards: tuple[list[bool], list[Path], int, int, int, list[bool]]


async def post_a_second_run(app: FastAPI) -> ClientSocket:
    second = ClientSocket()
    await respond_through(app, second)
    return second


def answer_status(client: ClientSocket) -> int:
    return client.messages[0]["status"]


HELD_WITH_THE_PLACE_TAKEN = (RunState.RUNNING, [False], 429)


async def stopped_run_held_then_released(
    app: FastAPI,
    drive: Drive,
    client: ClientSocket,
    response: asyncio.Task[None],
    held_reap: HeldReap,  # noqa: F811
    child_cleanup: ChildCleanup,
    config: ServiceConfig,
) -> StoppedRun:
    """Observe a stopped run while its reap is held and once it is released.

    The place is taken while held when a second run is refused ``busy``, and free
    after the release when the next run is admitted.
    """
    await settled_and_held(held_reap)
    refused = await post_a_second_run(app)
    while_held = (
        never_stopped_state(drive, client),
        all_children_gone(child_cleanup),
        answer_status(refused),
    )
    assert while_held == HELD_WITH_THE_PLACE_TAKEN
    held_reap.let_go.set()
    client.released.set()
    await response
    status = drive.status(client.run_id)
    assert status is not None
    after_the_stop = (
        all_children_gone(child_cleanup),
        list(private_root_of(config).iterdir()),
        held_claude_slots(),
        held_reap.reaps,
    )
    admitted = ClientSocket()
    admitted_response = await respond_through(app, admitted)
    await admitted.received(b"assistant_text")
    admitted.leave()
    await admitted_response
    afterwards = (*after_the_stop, answer_status(admitted), all_children_gone(child_cleanup))
    return StoppedRun(status, afterwards)


def the_place_is_free_after_one_reap(afterwards: tuple[Any, ...]) -> bool:
    return afterwards == ([True], [], 0, 1, 200, [True, True])


@pytest.mark.parametrize(
    ("runs", "blocks_the_client", "later_activity", "state", "failure_code", "client_lost"),
    [
        pytest.param({}, False, False, RunState.FAILED, FailureCode.IDLE_TIMEOUT, None, id="idle"),
        pytest.param(
            {"idle_timeout_s": 590.0},
            False,
            True,
            RunState.FAILED,
            FailureCode.LIMIT_EXCEEDED,
            None,
            id="maximum",
        ),
        pytest.param({}, True, False, RunState.CANCELLED, None, ClientLoss.DROPPED, id="stall"),
    ],
)
def test_every_stop_reaps_the_cli_once_before_the_record_ends(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,  # noqa: F811
    runs: dict[str, object],
    blocks_the_client: bool,
    later_activity: bool,
    state: RunState,
    failure_code: FailureCode | None,
    client_lost: ClientLoss | None,
) -> None:
    feed = tmp_path / "feed"
    os.mkfifo(feed)
    binary = a_parked_claude(
        tmp_path, for_the_client_socket=True, says_again_after=feed if later_activity else None
    )
    config = a_config(tmp_path, binary, runs={**runs, "max_parallel_cli_runs": 1})

    async def scenario() -> None:
        client = ClientSocket(blocks_the_second_chunk=blocks_the_client)
        async with driven(read=False) as drive:
            app = the_app_on(drive, monkeypatch, tool_free_runners(config), config)
            response = await respond_through(app, client)
            await (
                client.blocked.wait() if blocks_the_client else client.received(b"assistant_text")
            )
            if later_activity:
                await drive.run_to(20)
                await asyncio.to_thread(feed.write_text, "go\n")
                await client.received(b"again")
            await drive.run_to_end()
            stopped = await stopped_run_held_then_released(
                app, drive, client, response, held_reap, child_cleanup, config
            )
            assert (
                stopped.status.state,
                stopped.status.failure_code,
                stopped.status.client_lost,
            ) == (state, failure_code, client_lost)
            assert the_place_is_free_after_one_reap(stopped.afterwards)

    in_stepped_loop(child_cleanup, monkeypatch, scenario())


def test_a_stop_at_a_yielded_event_closes_the_core_at_once(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,  # noqa: F811
) -> None:
    binary = a_parked_claude(tmp_path, for_the_client_socket=True, stream=PARKED_AFTER_THREE_TEXTS)
    config = a_config(tmp_path, binary, runs={"max_parallel_cli_runs": 1})

    async def scenario() -> None:
        client = ClientSocket(blocks_the_second_chunk=True)
        async with driven(read=False) as drive:
            app = the_app_on(drive, monkeypatch, tool_free_runners(config), config)
            response = await respond_through(app, client)
            await client.blocked.wait()
            await asyncio.get_running_loop().settled()
            client.leave()
            stopped = await stopped_run_held_then_released(
                app, drive, client, response, held_reap, child_cleanup, config
            )
            assert (stopped.status.state, stopped.status.client_lost) == (
                RunState.CANCELLED,
                ClientLoss.DISCONNECTED,
            )
            assert the_place_is_free_after_one_reap(stopped.afterwards)

    in_stepped_loop(child_cleanup, monkeypatch, scenario())


def test_a_closed_runner_has_reaped_the_cli_before_its_close_returns(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_config(tmp_path, a_parked_claude(tmp_path, for_the_client_socket=True))

    async def scenario() -> tuple[AssistantTextEvent, int, list[Path], list[bool]]:
        runner = cli_runner.CliRunner(config, tls_context=None, clock=StepClock())
        events = runner.run(alice_admitted_run(uuid.uuid4()))
        first = await anext(events)
        await events.aclose()
        return (
            first,
            held_claude_slots(),
            list(private_root_of(config).iterdir()),
            all_children_gone(child_cleanup),
        )

    assert drive_real_children(child_cleanup, monkeypatch, scenario()) == (
        AssistantTextEvent(text="hi"),
        0,
        [],
        [True],
    )


def test_a_group_that_outlives_the_stop_keeps_the_runs_place_until_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = a_child_that_outlives_sigkill(
        censuses=(GroupCensus.LIVE, GroupCensus.EMPTY),
        stderr_stalls=True,
        stream_lines=[INIT_LINE, TEXT_LINE],
    )
    run = a_scripted_run(tmp_path)

    async def scenario() -> None:
        await anext(run.frames)
        await anext(run.frames)
        async with asyncio.timeout(TEST_WAIT_SECONDS / 4):
            await run.frames.aclose()
        status = run.status()
        assert (status.state, status.failure_code) == (RunState.FAILED, FailureCode.PROVIDER_ERROR)
        assert run.held_state() == (True, 1, True)
        await run.frees_its_place_once_the_group_is_confirmed(child)

    drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)


def test_an_unconfirmed_group_after_a_clean_end_never_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = a_claude_cli_child(
        stream_lines=[INIT_LINE, TEXT_LINE, SUCCESS_LINE],
        returncode=0,
        gone=(KillUntilGoneResult(GroupCensus.LIVE, ended_in_time=True),),
        censuses=(GroupCensus.LIVE, GroupCensus.EMPTY),
        stderr_stalls=True,
    )
    run = a_scripted_run(tmp_path)

    async def scenario() -> None:
        decoder = RunStreamDecoder()
        async with asyncio.timeout(TEST_WAIT_SECONDS / 4):
            async for frame in run.frames:
                decoder.feed(frame)
        status = run.status()
        assert (status.state, status.failure_code) == (RunState.FAILED, FailureCode.PROVIDER_ERROR)
        assert run.held_state() == (True, 1, True)
        await run.frees_its_place_once_the_group_is_confirmed(child)

    drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)


def test_the_service_entry_installs_the_cli_runner(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    logging_state_restored: None,
    authority: TestAuthority,
) -> None:
    binary = a_fake_claude(tmp_path, version_line=VERSION_LINE)
    config = trusting(a_config(tmp_path, binary, songmaker=PLAIN_APP), authority)

    events = through_the_service_entry(tmp_path, config, child_cleanup, monkeypatch, post_a_run)

    assert shown(events)[-1] == ("succeeded", "done")
