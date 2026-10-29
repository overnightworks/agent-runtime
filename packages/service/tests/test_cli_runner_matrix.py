"""The matrix cells of a CLI run that no other service module holds, through ``POST /runs``.

Rows serve the real app on a loopback socket and run the scripted Claude and Codex programs as
real children; the client's decoder reads every response. Test values: the Codex table with its
login, the listed model ``gpt-5.5`` and the pinned version 0.163.0.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from tests.support.anchored_support import ChildCleanup
from tests.support.codex_support import THREAD, a_message
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.provider_test_support import (
    INIT_LINE,
    SUCCESS_LINE,
    TEXT_LINE,
    a_fake_claude,
    line,
)
from tests.support.service_cli_runner_support import (
    THREE_MESSAGE_TURN,
    a_config,
    all_children_gone,
    decoded_in_chunks,
    drive_over_http,
    eventually,
    headers,
    leave_after,
    post_a_run,
    private_root_of,
    shown,
    status_of,
)
from tests.support.service_codex_support import (
    a_codex_config,
    a_codex_login_file,
    a_service_fake,
    codex_document,
)
from tests.support.service_support import (
    decoded_events,
    get_run,
    post_run,
    run_body,
)

from agent_runtime_client.wire import (
    MAX_EVENT_BYTES,
    MAX_RUN_FILE_NAME_BYTES,
    FailureCode,
    RunEvent,
    RunFile,
    RunState,
    RunStatus,
    Succeeded,
)
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.run_events import OUTPUT_REFUSED_MESSAGE

MARKER = "marker-4c1e"
PICTURE_FOLDER = '"$CODEX_HOME/generated_images/thread"'
WRITE_A_PICTURE = f'mkdir -p {PICTURE_FOLDER}; printf pixels > {PICTURE_FOLDER}/picture.png'
PARKED = "exec sleep 600"


def a_codex_run_config(tmp_path: Path, **fake: object) -> ServiceConfig:
    binary = a_service_fake(tmp_path, **fake)
    return a_codex_config(codex_document(tmp_path, binary, a_codex_login_file(tmp_path)))


def _files_of(events: list[RunEvent]) -> tuple[RunFile, ...]:
    terminal = events[-1]
    assert isinstance(terminal, Succeeded), terminal
    return terminal.files


@pytest.mark.parametrize(
    ("then", "files"),
    [
        pytest.param(WRITE_A_PICTURE, (RunFile(name="picture.png", content=b"pixels"),), id="one"),
        pytest.param(
            f"{WRITE_A_PICTURE}; printf a > output/a.txt; printf z > output/z.txt",
            (
                RunFile(name="a.txt", content=b"a"),
                RunFile(name="picture.png", content=b"pixels"),
                RunFile(name="z.txt", content=b"z"),
            ),
            id="beside-output-files-in-name-order",
        ),
        pytest.param("printf a > output/a.txt", (RunFile(name="a.txt", content=b"a"),), id="none"),
    ],
)
def test_a_codex_run_returns_its_pictures_with_its_output_files_and_leaves_nothing_behind(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    then: str,
    files: tuple[RunFile, ...],
) -> None:
    config = a_codex_run_config(tmp_path, then=then)

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    assert _files_of(events) == files
    assert list(private_root_of(config).iterdir()) == []


def test_a_link_in_the_picture_folder_ends_a_codex_run_refused_without_naming_it(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = a_codex_run_config(
        tmp_path, then=f"mkdir -p {PICTURE_FOLDER}; ln -s /etc/hostname {PICTURE_FOLDER}/{MARKER}"
    )

    def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(events), status.failure_code) == (
        [("assistant_text", "done"), ("failed", OUTPUT_REFUSED_MESSAGE)],
        FailureCode.PROVIDER_ERROR,
    )
    seen = f"{[event.model_dump_json() for event in events]} {status.model_dump_json()}"
    assert MARKER not in f"{seen} {caplog.text}"
    assert list(private_root_of(config).iterdir()) == []


def test_a_claude_run_with_the_clis_own_tool_lines_ends_succeeded_with_its_answer(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_call = line(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]
            },
        }
    )
    tool_result = line(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}]
            },
        }
    )
    stream = (INIT_LINE, tool_call, tool_result, TEXT_LINE, SUCCESS_LINE)
    config = a_config(tmp_path, a_fake_claude(tmp_path, stream))

    def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(events), status.state) == (
        [("assistant_text", "hi"), ("succeeded", "done")],
        RunState.SUCCEEDED,
    )


def test_a_codex_run_whose_client_leaves_ends_cancelled_with_its_group_gone_and_its_root_empty(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_codex_run_config(tmp_path, stream=(THREAD, a_message("one")), then=PARKED)

    def scenario(base_url: str) -> RunStatus:
        run_id = leave_after(base_url, "assistant_text")

        def read() -> RunStatus:
            return RunStatus.model_validate_json(get_run(base_url, headers(), str(run_id)).content)

        eventually(lambda: read().state is not RunState.RUNNING)
        return read()

    status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert status.state is RunState.CANCELLED
    assert all_children_gone(child_cleanup) == [True]
    assert list(private_root_of(config).iterdir()) == []


def a_claude_file_run(tmp_path: Path) -> ServiceConfig:
    return a_config(tmp_path, a_fake_claude(tmp_path, then="printf abc > output/note"))


def a_codex_picture_run(tmp_path: Path) -> ServiceConfig:
    return a_codex_run_config(tmp_path, then=WRITE_A_PICTURE)


@pytest.mark.parametrize("config_of", [a_claude_file_run, a_codex_picture_run])
@pytest.mark.parametrize(
    "chunk_sizes",
    [pytest.param((1,), id="one-byte"), pytest.param((1, 7, 2, 300, 3), id="uneven")],
)
def test_the_bytes_of_a_run_response_decode_to_the_same_events_in_any_split(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    config_of: Callable[[Path], ServiceConfig],
    chunk_sizes: tuple[int, ...],
) -> None:
    config = config_of(tmp_path)

    def scenario(base_url: str) -> bytes:
        return post_run(base_url, headers(), run_body(turn=THREE_MESSAGE_TURN)).content

    raw = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    whole = decoded_events(raw)
    assert _files_of(whole)
    assert decoded_in_chunks(raw, chunk_sizes) == whole


def test_files_whose_names_alone_pass_the_frame_cap_end_a_run_failed_as_a_provider_error(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = 4200
    padding = "x" * (MAX_RUN_FILE_NAME_BYTES - len(str(names)))
    config = a_config(
        tmp_path,
        a_fake_claude(
            tmp_path,
            then=(
                f"i=0; while [ $i -lt {names} ]; do "
                f': > "output/$(printf %0{len(str(names))}d $i){padding}"; i=$((i+1)); done'
            ),
        ),
    )

    def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(events)[-1], status.failure_code) == (
        ("failed", "The provider's succeeded cannot be sent."),
        FailureCode.PROVIDER_ERROR,
    )
    assert "succeeded" not in [event.type for event in events]
    assert MAX_EVENT_BYTES < names * MAX_RUN_FILE_NAME_BYTES
    assert list(private_root_of(config).iterdir()) == []
