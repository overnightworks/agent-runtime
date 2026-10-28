"""A run's output folder is read once, after the group is gone, as single-named plain files.

The collector rows run on a prepared folder; the ``astream_cli_run`` rows start a ``/bin/sh``
leader through the real door that writes into the folder.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import threading
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.support.anchored_leaders import spawn_warnings
from tests.support.anchored_support import GONE, ChildCleanup
from tests.support.bounds import TEST_WAIT_SECONDS
from tests.support.provider_test_support import (
    REAL_REAP_BOUNDS,
    a_private_root,
    drive_real_children,
    fail_the_directory_named,
    held_slots,
)

import agent_runtime.run_output as run_output
from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.cli_run import CliRunStart, StepEvent, astream_cli_run
from agent_runtime.config import PrivateWorkingRoot, reset_config
from agent_runtime.events import FinalEvent, StreamEvent
from agent_runtime.process import (
    CliEnvironmentCause,
    CliEnvironmentUnavailableError,
    RunDirectories,
    run_directories,
)
from agent_runtime.run_output import RunOutputCause, RunOutputRefused, collect_output
from agent_runtime.spawn import ChildProcess, closed_environment
from agent_runtime_client.wire import RunFile

SHELL = Path("/bin/sh")
LIMIT = 16
MARKER = "marker-4c1f9a"
ONE_TEBIBYTE = 1024**4


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret").write_bytes(b"secret")
    return folder


@pytest.fixture
def directories(tmp_path: Path) -> Iterator[RunDirectories]:
    with run_directories(PrivateWorkingRoot(a_private_root(tmp_path)), {}) as made:
        yield made


def returns_within_the_bound[T](call: Callable[[], T]) -> T:
    """Run a synchronous call and fail the test by name when it does not return in time."""
    outcome: list[T] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as error:  # noqa: BLE001 - handed to the test thread below
            failures.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(TEST_WAIT_SECONDS)
    assert not worker.is_alive(), "the collector did not return within the test bound"
    if failures:
        raise failures[0]
    return outcome[0]


def refusal_of(directories: RunDirectories, limit: int = LIMIT) -> RunOutputRefused:
    with pytest.raises(RunOutputRefused) as refused:
        returns_within_the_bound(lambda: collect_output(directories, limit))
    return refused.value


def test_a_run_that_writes_nothing_returns_no_files(directories: RunDirectories) -> None:
    assert collect_output(directories, LIMIT) == ()


def test_a_file_comes_back_with_its_name_and_exact_bytes(directories: RunDirectories) -> None:
    (directories.output / "report").write_bytes(b"\x00\xffbytes\n")

    assert collect_output(directories, LIMIT) == (
        RunFile(name="report", content=b"\x00\xffbytes\n"),
    )


def test_several_files_come_back_in_name_order(directories: RunDirectories) -> None:
    for name in ("b", "c", "a"):
        (directories.output / name).write_bytes(name.encode())

    assert [file.name for file in collect_output(directories, LIMIT)] == ["a", "b", "c"]


def test_an_empty_file_comes_back_with_empty_content(directories: RunDirectories) -> None:
    (directories.output / "empty").write_bytes(b"")

    assert collect_output(directories, LIMIT) == (RunFile(name="empty", content=b""),)


@pytest.mark.parametrize(
    ("sizes", "limit"),
    [
        pytest.param((8, 8), LIMIT, id="exactly-the-limit"),
        pytest.param((0,), 0, id="limit-zero-with-an-empty-file"),
    ],
)
def test_files_totalling_the_limit_are_accepted(
    directories: RunDirectories, sizes: tuple[int, ...], limit: int,
) -> None:
    for index, size in enumerate(sizes):
        (directories.output / f"file-{index}").write_bytes(b"x" * size)

    assert [len(file.content) for file in collect_output(directories, limit)] == list(sizes)


def test_files_totalling_one_byte_over_the_limit_are_refused(directories: RunDirectories) -> None:
    (directories.output / "a").write_bytes(b"x" * 8)
    (directories.output / "b").write_bytes(b"x" * 9)

    assert refusal_of(directories).cause is RunOutputCause.OVER_LIMIT


def test_a_huge_sparse_file_after_a_small_one_is_refused_after_reading_the_limit_plus_one_in_all(
    directories: RunDirectories, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (directories.output / "a").write_bytes(b"x" * 5)
    with (directories.output / "huge").open("wb") as huge:
        os.truncate(huge.fileno(), ONE_TEBIBYTE)
    bytes_read: list[int] = []
    real_read = os.read

    def counting_read(descriptor: int, length: int) -> bytes:
        chunk = real_read(descriptor, length)
        bytes_read.append(len(chunk))
        return chunk

    monkeypatch.setattr(run_output.os, "read", counting_read)

    cause = refusal_of(directories).cause

    assert (cause, sum(bytes_read)) == (RunOutputCause.OVER_LIMIT, LIMIT + 1)


def a_link_to_a_file(output: Path, outside: Path) -> None:
    (output / MARKER).symlink_to(outside / "secret")


def a_link_to_a_fifo(output: Path, outside: Path) -> None:
    os.mkfifo(outside / "pipe")
    (output / MARKER).symlink_to(outside / "pipe")


def the_folder_replaced_by_a_link(output: Path, outside: Path) -> None:
    shutil.rmtree(output)
    output.symlink_to(outside)


def the_folder_removed(output: Path, outside: Path) -> None:
    output.rmdir()


def the_folder_replaced_by_a_file(output: Path, outside: Path) -> None:
    output.rmdir()
    output.write_bytes(MARKER.encode())


def the_work_directory_replaced_by_a_link(output: Path, outside: Path) -> None:
    moved = outside / "moved-work"
    output.parent.rename(moved)
    output.parent.symlink_to(moved)


def two_names_for_one_file(output: Path, outside: Path) -> None:
    (output / f"{MARKER}-a").write_bytes(MARKER.encode())
    os.link(output / f"{MARKER}-a", output / f"{MARKER}-b")


def a_name_linked_to_an_outside_file(output: Path, outside: Path) -> None:
    os.link(outside / "secret", output / MARKER)


def a_fifo_without_a_writer(output: Path, outside: Path) -> None:
    os.mkfifo(output / MARKER)


def a_subdirectory(output: Path, outside: Path) -> None:
    (output / MARKER).mkdir()


def a_unix_socket(output: Path, outside: Path) -> None:
    previous = os.getcwd()
    os.chdir(output)
    try:
        with socket.socket(socket.AF_UNIX) as bound:
            bound.bind(MARKER)
    finally:
        os.chdir(previous)


def a_name_that_is_not_utf8(output: Path, outside: Path) -> None:
    (Path(os.fsdecode(os.fsencode(output) + b"/\xff" + MARKER.encode()))).write_bytes(b"x")


def a_file_nobody_may_read(output: Path, outside: Path) -> None:
    unreadable = output / MARKER
    unreadable.write_bytes(MARKER.encode())
    unreadable.chmod(0)


def two_files_over_the_limit(output: Path, outside: Path) -> None:
    (output / f"{MARKER}-a").write_bytes(MARKER.encode())
    (output / f"{MARKER}-b").write_bytes(MARKER.encode())


REFUSALS = [
    pytest.param(RunOutputCause.LINK, a_link_to_a_file, id="link-to-a-file"),
    pytest.param(RunOutputCause.LINK, a_link_to_a_fifo, id="link-to-a-fifo"),
    pytest.param(RunOutputCause.FOLDER_REPLACED, the_folder_replaced_by_a_link, id="folder-link"),
    pytest.param(RunOutputCause.FOLDER_REPLACED, the_folder_removed, id="folder-removed"),
    pytest.param(RunOutputCause.FOLDER_REPLACED, the_folder_replaced_by_a_file, id="folder-file"),
    pytest.param(
        RunOutputCause.FOLDER_REPLACED, the_work_directory_replaced_by_a_link, id="work-link"
    ),
    pytest.param(RunOutputCause.SECOND_NAME, two_names_for_one_file, id="two-names"),
    pytest.param(
        RunOutputCause.SECOND_NAME, a_name_linked_to_an_outside_file, id="outside-hard-link"
    ),
    pytest.param(RunOutputCause.NOT_A_REGULAR_FILE, a_fifo_without_a_writer, id="fifo"),
    pytest.param(RunOutputCause.NOT_A_REGULAR_FILE, a_subdirectory, id="subdirectory"),
    pytest.param(RunOutputCause.NOT_A_REGULAR_FILE, a_unix_socket, id="socket"),
    pytest.param(RunOutputCause.NAME_REFUSED, a_name_that_is_not_utf8, id="name-not-utf8"),
    pytest.param(RunOutputCause.UNREADABLE, a_file_nobody_may_read, id="mode-000"),
    pytest.param(RunOutputCause.OVER_LIMIT, two_files_over_the_limit, id="over-the-limit"),
]


@pytest.mark.parametrize(("cause", "prepare"), REFUSALS)
def test_everything_but_a_single_named_regular_file_is_refused_with_its_cause(
    directories: RunDirectories,
    outside: Path,
    cause: RunOutputCause,
    prepare: Callable[[Path, Path], None],
) -> None:
    prepare(directories.output, outside)

    assert refusal_of(directories).cause is cause


@pytest.mark.parametrize(("cause", "prepare"), REFUSALS)
def test_a_refusal_carries_no_name_path_or_content_in_its_message_log_or_chain(
    directories: RunDirectories,
    outside: Path,
    caplog: pytest.LogCaptureFixture,
    cause: RunOutputCause,
    prepare: Callable[[Path, Path], None],
) -> None:
    prepare(directories.output, outside)

    with caplog.at_level(logging.DEBUG):
        refusal = refusal_of(directories)

    assert (
        MARKER in str(refusal) or MARKER in repr(refusal) or MARKER in caplog.text,
        refusal.__cause__,
        refusal.__context__,
        cause.name in caplog.text,
    ) == (False, None, None, True)


@dataclass
class ScriptAdapter:
    pool: CliProcessPool
    script: str
    returncodes_refused: bool = False
    line_limit_bytes: int = 1024

    def run_files(self) -> dict[str, bytes]:
        return {}

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        child = ChildProcess(
            binary=SHELL,
            arguments=("-c", self.script),
            environment=closed_environment(home),
            working_directory=working_directory,
        )
        return CliRunStart(child, b"", EndingDecoder())


class DecoderRefused(Exception):
    """The decoder's own failure for a run that did not end cleanly."""


class EndingDecoder:
    def events_of(self, line: bytes) -> list[StepEvent]:
        return []

    def ending(self, returncode: int) -> FinalEvent:
        if returncode != 0:
            raise DecoderRefused
        return FinalEvent(text="done")


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    return a_private_root(tmp_path)


@pytest.fixture
def root(private_root: Path) -> PrivateWorkingRoot:
    return PrivateWorkingRoot(private_root)


@pytest.fixture
def pool() -> CliProcessPool:
    return CliProcessPool(limit=1, name="run output test", reap=REAL_REAP_BOUNDS)


@pytest.fixture
def drive(
    pool: CliProcessPool,
    child_cleanup: ChildCleanup,
    thread_failures: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[Callable[[Coroutine[Any, Any, Any]], Any]]:
    yield lambda scenario: drive_real_children(child_cleanup, monkeypatch, scenario)
    assert [child_cleanup.state(owned) is GONE for owned in child_cleanup.listed()] == [
        True for _ in child_cleanup.listed()
    ]
    assert (held_slots(pool), thread_failures, spawn_warnings(caplog)) == (0, [], [])


async def events_of(
    root: PrivateWorkingRoot, adapter: ScriptAdapter, limit: int = LIMIT,
) -> list[StreamEvent]:
    return [event async for event in astream_cli_run(root, adapter, max_files_bytes=limit)]


WRITES_TWO_FILES = "printf abc > output/a; : > output/b\n"


def test_the_files_arrive_in_the_ending_after_the_group_is_gone_and_the_tree_removed(
    drive, root: PrivateWorkingRoot, private_root: Path, pool: CliProcessPool,
) -> None:
    adapter = ScriptAdapter(pool, WRITES_TWO_FILES)
    at_the_ending: list[tuple[int, list[Path]]] = []

    async def scenario() -> list[StreamEvent]:
        seen = []
        async for event in astream_cli_run(root, adapter, max_files_bytes=LIMIT):
            seen.append(event)
            at_the_ending.append((held_slots(pool), list(private_root.iterdir())))
        return seen

    assert drive(scenario()) == [
        FinalEvent(
            text="done",
            files=(RunFile(name="a", content=b"abc"), RunFile(name="b", content=b"")),
        )
    ]
    assert at_the_ending == [(0, [])]


def test_a_run_gets_an_empty_output_folder_at_start_and_a_run_that_writes_nothing_has_no_files(
    drive, root: PrivateWorkingRoot, pool: CliProcessPool,
) -> None:
    adapter = ScriptAdapter(pool, 'test -d output && test -z "$(ls -A output)"\n')

    assert drive(events_of(root, adapter)) == [FinalEvent(text="done", files=())]


def test_a_refused_collection_ends_the_run_with_its_cause_and_leaves_no_tree(
    drive, root: PrivateWorkingRoot, private_root: Path, pool: CliProcessPool,
) -> None:
    adapter = ScriptAdapter(pool, "ln -s /nonexistent output/link\n")

    async def scenario() -> RunOutputCause:
        with pytest.raises(RunOutputRefused) as refused:
            await events_of(root, adapter)
        return refused.value.cause

    assert drive(scenario()) is RunOutputCause.LINK
    assert list(private_root.iterdir()) == []


def test_a_decoder_that_refuses_the_ending_is_judged_before_anything_is_read(
    drive, root: PrivateWorkingRoot, pool: CliProcessPool,
) -> None:
    adapter = ScriptAdapter(pool, "mkfifo output/pipe; exit 3\n")

    async def scenario() -> None:
        with pytest.raises(DecoderRefused):
            await events_of(root, adapter)

    drive(scenario())


def test_a_run_collects_its_files_without_installed_configuration(
    drive, root: PrivateWorkingRoot, pool: CliProcessPool,
) -> None:
    reset_config()
    adapter = ScriptAdapter(pool, WRITES_TWO_FILES)

    (final,) = drive(events_of(root, adapter))

    assert [file.name for file in final.files] == ["a", "b"]


@pytest.mark.parametrize("limit", [-1, True, 1.0], ids=["negative", "bool", "float"])
def test_a_limit_that_is_not_a_non_negative_int_raises_before_any_tree_or_child_exists(
    drive,
    root: PrivateWorkingRoot,
    private_root: Path,
    pool: CliProcessPool,
    child_cleanup: ChildCleanup,
    limit: object,
) -> None:
    adapter = ScriptAdapter(pool, WRITES_TWO_FILES)

    async def scenario() -> None:
        events = astream_cli_run(root, adapter, max_files_bytes=limit)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_files_bytes"):
            await anext(events)

    drive(scenario())

    assert (list(private_root.iterdir()), child_cleanup.listed()) == ([], ())


def test_a_failing_output_folder_creation_becomes_the_environment_error_and_leaves_no_tree(
    private_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_the_directory_named(monkeypatch, "output", OSError("no space"))

    with pytest.raises(CliEnvironmentUnavailableError) as refused:
        with run_directories(PrivateWorkingRoot(private_root), {}):
            pytest.fail("the run must not start")

    assert (refused.value.cause, list(private_root.iterdir())) == (
        CliEnvironmentCause.HOME_NOT_CREATED, [],
    )
