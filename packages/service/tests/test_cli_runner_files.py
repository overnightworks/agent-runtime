"""The files a CLI run leaves in its output folder come back inside the ``succeeded`` event.

Rows serve the real app on a loopback socket and run a scripted Claude as a real child whose
last step writes into ``output/``; the client's decoder reads the response.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.anchored_support import ChildCleanup
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.provider_test_support import INIT_LINE, a_fake_claude, line
from tests.support.service_cli_runner_support import (
    a_config,
    drive_over_http,
    post_a_run,
    private_root_of,
    shown,
    status_of,
)

from agent_runtime_client.sse import encode_event
from agent_runtime_client.wire import (
    MAX_RUN_FILES_BYTES,
    MAX_SUCCEEDED_FRAME_BYTES,
    FailureCode,
    RunEvent,
    RunFile,
    RunStatus,
    Succeeded,
)
from agent_runtime_service.run_events import (
    INVALID_EVENT_MESSAGE,
    OUTPUT_REFUSED_MESSAGE,
    TOO_MUCH_DATA_MESSAGE,
)

MARKER = "marker-7f3a"
NO_WORDS = (
    INIT_LINE,
    line({"type": "result", "subtype": "success", "is_error": False, "result": ""}),
)
EIGHT_MEBIBYTES = 8 * 1024 * 1024
OVER_LIMIT = ("failed", TOO_MUCH_DATA_MESSAGE)


def _driven(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    then: str,
    *,
    stream: tuple[bytes, ...] | None = None,
    runs: dict[str, object] | None = None,
) -> tuple[list[RunEvent], RunStatus, Path]:
    options = {} if stream is None else {"stream": stream}
    config = a_config(tmp_path, a_fake_claude(tmp_path, then=then, **options), runs=runs)

    def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)
    return events, status, private_root_of(config)


def _files_of(events: list[RunEvent]) -> tuple[RunFile, ...]:
    terminal = events[-1]
    assert isinstance(terminal, Succeeded), terminal
    return terminal.files


@pytest.mark.parametrize(
    ("then", "files"),
    [
        pytest.param("exit 0", (), id="none"),
        pytest.param("printf abc > output/note", (RunFile(name="note", content=b"abc"),), id="one"),
        pytest.param(
            "printf b > output/b; printf a > output/a",
            (RunFile(name="a", content=b"a"), RunFile(name="b", content=b"b")),
            id="several-in-name-order",
        ),
        pytest.param(": > output/empty", (RunFile(name="empty", content=b""),), id="empty-file"),
    ],
)
def test_a_run_returns_every_file_of_its_output_folder_and_leaves_nothing_behind(
    tmp_path, child_cleanup, monkeypatch, then, files
):
    events, _status, root = _driven(tmp_path, child_cleanup, monkeypatch, then)

    assert _files_of(events) == files
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    ("size", "ending"),
    [
        pytest.param(1024, "succeeded", id="exactly-the-limit"),
        pytest.param(1025, "failed", id="one-byte-over"),
    ],
)
def test_the_configured_limit_decides_whether_a_runs_files_come_back(
    tmp_path, child_cleanup, monkeypatch, size, ending
):
    events, _status, root = _driven(
        tmp_path,
        child_cleanup,
        monkeypatch,
        f"truncate -s {size} output/note",
        runs={"max_files_bytes": 1024},
    )

    assert events[-1].type == ending
    if ending == "failed":
        assert shown(events)[-1] == OVER_LIMIT
    else:
        (note,) = _files_of(events)
        assert len(note.content) == size
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    ("size", "ending"),
    [
        pytest.param(EIGHT_MEBIBYTES, "succeeded", id="exactly-the-default"),
        pytest.param(EIGHT_MEBIBYTES + 1, "failed", id="one-byte-over-the-default"),
    ],
)
def test_a_run_without_a_configured_limit_is_held_to_eight_mebibytes(
    tmp_path, child_cleanup, monkeypatch, size, ending
):
    events, _status, _root = _driven(
        tmp_path, child_cleanup, monkeypatch, f"truncate -s {size} output/note"
    )

    assert events[-1].type == ending
    if ending == "failed":
        assert shown(events)[-1] == OVER_LIMIT


def test_a_run_at_the_wire_ceiling_returns_a_file_of_that_size_in_one_frame(
    tmp_path, child_cleanup, monkeypatch
):
    events, _status, _root = _driven(
        tmp_path,
        child_cleanup,
        monkeypatch,
        f"truncate -s {MAX_RUN_FILES_BYTES} output/big",
        runs={"max_files_bytes": MAX_RUN_FILES_BYTES},
    )

    (big,) = _files_of(events)
    assert (big.name, len(big.content)) == ("big", MAX_RUN_FILES_BYTES)
    assert len(encode_event(events[-1])) <= MAX_SUCCEEDED_FRAME_BYTES


def test_a_run_whose_words_are_empty_succeeds_beside_a_file(tmp_path, child_cleanup, monkeypatch):
    events, _status, _root = _driven(
        tmp_path, child_cleanup, monkeypatch, "printf abc > output/note", stream=NO_WORDS
    )

    assert events[-1].text == ""
    assert _files_of(events) == (RunFile(name="note", content=b"abc"),)


def test_a_run_without_words_or_a_file_ends_as_an_invalid_event(
    tmp_path, child_cleanup, monkeypatch
):
    events, status, _root = _driven(tmp_path, child_cleanup, monkeypatch, "exit 0", stream=NO_WORDS)

    assert (shown(events), status.failure_code) == (
        [("failed", INVALID_EVENT_MESSAGE)],
        FailureCode.PROVIDER_ERROR,
    )


@pytest.mark.parametrize(
    "then",
    [
        pytest.param(f"ln -s /etc/hostname output/{MARKER}", id="link"),
        pytest.param(f"printf {MARKER} > output/a; ln output/a output/{MARKER}-b", id="hard-link"),
        pytest.param(f"mkdir output/{MARKER}", id="subdirectory"),
        pytest.param(f"mkfifo output/{MARKER}", id="fifo"),
        pytest.param(
            f"printf {MARKER} > output/{MARKER}; chmod 000 output/{MARKER}", id="mode-000"
        ),
        pytest.param(
            f"printf {MARKER} > \"output/{MARKER}$(printf '\\377')\"", id="non-utf8-name"
        ),
        pytest.param(f"rm -r output; ln -s {MARKER} output", id="folder-replaced-by-a-link"),
    ],
)
def test_a_refused_output_entry_ends_the_run_after_its_earlier_frames_without_naming_it(
    tmp_path, child_cleanup, monkeypatch, caplog, then
):
    events, status, root = _driven(tmp_path, child_cleanup, monkeypatch, then)

    assert (shown(events), status.failure_code) == (
        [("assistant_text", "hi"), ("failed", OUTPUT_REFUSED_MESSAGE)],
        FailureCode.PROVIDER_ERROR,
    )
    seen = f"{[event.model_dump_json() for event in events]} {status.model_dump_json()}"
    assert MARKER not in f"{seen} {caplog.text}"
    assert list(root.iterdir()) == []


def test_the_run_record_of_a_file_run_holds_no_file_name_or_content(
    tmp_path, child_cleanup, monkeypatch
):
    events, status, _root = _driven(
        tmp_path, child_cleanup, monkeypatch, f"printf {MARKER} > output/{MARKER}"
    )

    assert _files_of(events) == (RunFile(name=MARKER, content=MARKER.encode()),)
    assert MARKER not in status.model_dump_json()
