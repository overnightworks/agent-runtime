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
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.support.anchored_support import ChildCleanup
from tests.support.bounds import TEST_WAIT_SECONDS
from tests.support.cli_run_support import (
    EndingRefused,
    LineScriptAdapter,
    a_drive,  # noqa: F401 - the fixture is named in its decorator
    a_pool,  # noqa: F401 - the fixture is named in its decorator
    a_private_working_root,  # noqa: F401 - the fixture is named in its decorator
    a_root,  # noqa: F401 - the fixture is named in its decorator
)
from tests.support.provider_test_support import (
    a_private_root,
    fail_the_directory_named,
    held_slots,
)

import agent_runtime.run_output as run_output
from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.cli_run import astream_cli_run
from agent_runtime.config import PrivateWorkingRoot, reset_config
from agent_runtime.events import FinalEvent, StreamEvent
from agent_runtime.process import (
    CliEnvironmentCause,
    CliEnvironmentUnavailableError,
    RunDirectories,
    run_directories,
)
from agent_runtime.run_output import RunOutputCause, RunOutputRefused, collect_output
from agent_runtime_client.wire import RunFile

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
        try:
            yield made
        finally:
            for folder in (made.home, made.home / PICTURES[0]):
                if folder.is_dir() and not folder.is_symlink():
                    folder.chmod(0o700)


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


def a_script_that_ends_strictly(pool: CliProcessPool, script: str) -> LineScriptAdapter:
    return LineScriptAdapter(pool, script, refuses_failed_ending=True)


async def events_of(
    root: PrivateWorkingRoot, adapter: LineScriptAdapter, limit: int = LIMIT,
) -> list[StreamEvent]:
    return [event async for event in astream_cli_run(root, adapter, max_files_bytes=limit)]


WRITES_TWO_FILES = "printf abc > output/a; : > output/b\n"


def test_the_files_arrive_in_the_ending_after_the_group_is_gone_and_the_tree_removed(
    drive, root: PrivateWorkingRoot, private_root: Path, pool: CliProcessPool,
) -> None:
    adapter = a_script_that_ends_strictly(pool, WRITES_TWO_FILES)
    at_the_ending: list[tuple[int, list[Path]]] = []

    async def scenario() -> list[StreamEvent]:
        seen = []
        async for event in astream_cli_run(root, adapter, max_files_bytes=LIMIT):
            seen.append(event)
            at_the_ending.append((held_slots(pool), list(private_root.iterdir())))
        return seen

    assert drive(scenario()) == [
        FinalEvent(
            text="Final rc=0",
            files=(RunFile(name="a", content=b"abc"), RunFile(name="b", content=b"")),
        )
    ]
    assert at_the_ending == [(0, [])]


def test_a_run_gets_an_empty_output_folder_at_start_and_a_run_that_writes_nothing_has_no_files(
    drive, root: PrivateWorkingRoot, pool: CliProcessPool,
) -> None:
    adapter = a_script_that_ends_strictly(pool, 'test -d output && test -z "$(ls -A output)"\n')

    assert drive(events_of(root, adapter)) == [FinalEvent(text="Final rc=0", files=())]


def test_a_refused_collection_ends_the_run_with_its_cause_and_leaves_no_tree(
    drive, root: PrivateWorkingRoot, private_root: Path, pool: CliProcessPool,
) -> None:
    adapter = a_script_that_ends_strictly(pool, "ln -s /nonexistent output/link\n")

    async def scenario() -> RunOutputCause:
        with pytest.raises(RunOutputRefused) as refused:
            await events_of(root, adapter)
        return refused.value.cause

    assert drive(scenario()) is RunOutputCause.LINK
    assert list(private_root.iterdir()) == []


def test_a_decoder_that_refuses_the_ending_is_judged_before_anything_is_read(
    drive, root: PrivateWorkingRoot, pool: CliProcessPool,
) -> None:
    adapter = a_script_that_ends_strictly(pool, "mkfifo output/pipe; exit 3\n")

    async def scenario() -> None:
        with pytest.raises(EndingRefused):
            await events_of(root, adapter)

    drive(scenario())


def test_a_run_collects_its_files_without_installed_configuration(
    drive, root: PrivateWorkingRoot, pool: CliProcessPool,
) -> None:
    reset_config()
    adapter = a_script_that_ends_strictly(pool, WRITES_TWO_FILES)

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
    adapter = a_script_that_ends_strictly(pool, WRITES_TWO_FILES)

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
    working_root = PrivateWorkingRoot(private_root)

    entering = run_directories(working_root, {})
    with pytest.raises(CliEnvironmentUnavailableError) as refused:
        entering.__enter__()

    assert (refused.value.cause, list(private_root.iterdir())) == (
        CliEnvironmentCause.HOME_NOT_CREATED, [],
    )


PICTURES = ("generated_images",)
PNG = b"\x89PNG-bytes"


def a_picture(directories: RunDirectories, thread: str, name: str, content: bytes = PNG) -> Path:
    folder = directories.home / PICTURES[0] / thread
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(content)
    return folder / name


def pictures_of(directories: RunDirectories, limit: int = LIMIT) -> tuple[RunFile, ...]:
    return returns_within_the_bound(lambda: collect_output(directories, limit, PICTURES))


def picture_refusal_of(directories: RunDirectories, limit: int = LIMIT) -> RunOutputRefused:
    with pytest.raises(RunOutputRefused) as refused:
        returns_within_the_bound(lambda: collect_output(directories, limit, PICTURES))
    return refused.value


def test_an_absent_picture_folder_adds_nothing_and_refuses_nothing(
    directories: RunDirectories,
) -> None:
    (directories.output / "report").write_bytes(b"r")

    assert pictures_of(directories) == (RunFile(name="report", content=b"r"),)


def test_a_picture_in_a_thread_folder_comes_back_among_the_files(
    directories: RunDirectories,
) -> None:
    a_picture(directories, "thread", "exec-1.png")

    assert pictures_of(directories) == (RunFile(name="exec-1.png", content=PNG),)


def test_pictures_of_two_threads_and_the_output_files_come_back_in_one_name_order(
    directories: RunDirectories,
) -> None:
    a_picture(directories, "thread-b", "c.png", b"c")
    a_picture(directories, "thread-a", "a.png", b"a")
    (directories.output / "b.txt").write_bytes(b"b")

    assert [file.name for file in pictures_of(directories)] == ["a.png", "b.txt", "c.png"]


def test_a_collection_without_picture_folders_does_not_read_the_home(
    directories: RunDirectories,
) -> None:
    a_picture(directories, "thread", "exec-1.png")

    assert collect_output(directories, LIMIT) == ()


def test_the_rest_of_the_home_is_not_returned(directories: RunDirectories) -> None:
    shipped = directories.home / "skills" / ".system" / "skill"
    shipped.mkdir(parents=True)
    (shipped / "icon.png").write_bytes(PNG)

    assert pictures_of(directories) == ()


def a_picture_folder_that_is_a_link(directories: RunDirectories, outside: Path) -> None:
    (directories.home / PICTURES[0]).symlink_to(outside)


def a_picture_folder_that_is_a_file(directories: RunDirectories, outside: Path) -> None:
    (directories.home / PICTURES[0]).write_bytes(MARKER.encode())


def a_home_replaced_by_a_link(directories: RunDirectories, outside: Path) -> None:
    moved = outside / f"{MARKER}-home"
    directories.home.rename(moved)
    directories.home.symlink_to(moved)


def a_thread_entry_that_is_a_link_to_a_folder(directories: RunDirectories, outside: Path) -> None:
    (directories.home / PICTURES[0]).mkdir()
    (directories.home / PICTURES[0] / MARKER).symlink_to(outside)


def a_thread_entry_that_is_a_file(directories: RunDirectories, outside: Path) -> None:
    (directories.home / PICTURES[0]).mkdir()
    (directories.home / PICTURES[0] / MARKER).write_bytes(MARKER.encode())


def a_picture_that_is_a_link(directories: RunDirectories, outside: Path) -> None:
    a_picture(directories, "thread", "x").unlink()
    (directories.home / PICTURES[0] / "thread" / MARKER).symlink_to(outside / "secret")


def a_picture_that_is_a_fifo(directories: RunDirectories, outside: Path) -> None:
    a_picture(directories, "thread", "x").unlink()
    os.mkfifo(directories.home / PICTURES[0] / "thread" / MARKER)


def a_picture_that_is_a_subfolder(directories: RunDirectories, outside: Path) -> None:
    a_picture(directories, "thread", "x").unlink()
    (directories.home / PICTURES[0] / "thread" / MARKER).mkdir()
    (directories.home / PICTURES[0] / "thread" / MARKER / MARKER).write_bytes(b"x")


def a_picture_with_a_second_name(directories: RunDirectories, outside: Path) -> None:
    os.link(a_picture(directories, "thread", MARKER), outside / "second-name")


def a_name_in_the_output_and_a_thread(directories: RunDirectories, outside: Path) -> None:
    (directories.output / MARKER).write_bytes(b"1")
    a_picture(directories, "thread", MARKER, b"2")


def a_name_in_two_threads(directories: RunDirectories, outside: Path) -> None:
    a_picture(directories, "thread-a", MARKER, b"1")
    a_picture(directories, "thread-b", MARKER, b"2")


def pictures_over_the_limit_with_the_output(directories: RunDirectories, outside: Path) -> None:
    (directories.output / "a").write_bytes(b"x" * 8)
    a_picture(directories, "thread", MARKER, b"x" * 9)


def a_home_that_is_not_searchable(directories: RunDirectories, outside: Path) -> None:
    a_picture(directories, "thread", MARKER)
    directories.home.chmod(0o600)


def a_picture_folder_that_is_not_searchable(directories: RunDirectories, outside: Path) -> None:
    a_picture(directories, "thread", MARKER)
    (directories.home / PICTURES[0]).chmod(0o600)


PICTURE_REFUSALS = [
    pytest.param(RunOutputCause.FOLDER_REPLACED, a_picture_folder_that_is_a_link, id="folder-link"),
    pytest.param(RunOutputCause.FOLDER_REPLACED, a_picture_folder_that_is_a_file, id="folder-file"),
    pytest.param(RunOutputCause.FOLDER_REPLACED, a_home_replaced_by_a_link, id="home-link"),
    pytest.param(
        RunOutputCause.FOLDER_REPLACED, a_thread_entry_that_is_a_link_to_a_folder, id="thread-link"
    ),
    pytest.param(RunOutputCause.FOLDER_REPLACED, a_thread_entry_that_is_a_file, id="thread-file"),
    pytest.param(RunOutputCause.LINK, a_picture_that_is_a_link, id="picture-link"),
    pytest.param(RunOutputCause.NOT_A_REGULAR_FILE, a_picture_that_is_a_fifo, id="picture-fifo"),
    pytest.param(
        RunOutputCause.NOT_A_REGULAR_FILE, a_picture_that_is_a_subfolder, id="picture-subfolder"
    ),
    pytest.param(RunOutputCause.SECOND_NAME, a_picture_with_a_second_name, id="second-name"),
    pytest.param(
        RunOutputCause.NAME_PRESENT_TWICE, a_name_in_the_output_and_a_thread, id="twice-output"
    ),
    pytest.param(RunOutputCause.NAME_PRESENT_TWICE, a_name_in_two_threads, id="twice-threads"),
    pytest.param(
        RunOutputCause.OVER_LIMIT, pictures_over_the_limit_with_the_output, id="over-the-limit"
    ),
    pytest.param(RunOutputCause.UNREADABLE, a_home_that_is_not_searchable, id="home-unsearchable"),
    pytest.param(
        RunOutputCause.UNREADABLE, a_picture_folder_that_is_not_searchable, id="folder-unsearchable"
    ),
]


@pytest.mark.parametrize(("cause", "prepare"), PICTURE_REFUSALS)
def test_a_picture_that_is_not_a_single_named_regular_file_in_a_thread_folder_is_refused(
    directories: RunDirectories,
    outside: Path,
    caplog: pytest.LogCaptureFixture,
    cause: RunOutputCause,
    prepare: Callable[[RunDirectories, Path], None],
) -> None:
    prepare(directories, outside)

    with caplog.at_level(logging.DEBUG):
        refusal = picture_refusal_of(directories)

    assert (
        refusal.cause,
        MARKER in str(refusal) or MARKER in repr(refusal) or MARKER in caplog.text,
        refusal.__cause__,
        refusal.__context__,
    ) == (cause, False, None, None)


def test_the_output_and_the_pictures_exactly_at_the_limit_are_accepted(
    directories: RunDirectories,
) -> None:
    (directories.output / "a").write_bytes(b"x" * 8)
    a_picture(directories, "thread", "b", b"x" * 8)

    assert [len(file.content) for file in pictures_of(directories)] == [8, 8]


def test_a_picture_folder_is_read_only_when_the_adapter_names_it(
    drive, root: PrivateWorkingRoot, pool: CliProcessPool,
) -> None:
    script = "mkdir -p ../home/generated_images/t; printf p > ../home/generated_images/t/pic\n"
    named = LineScriptAdapter(pool, script, picture_folders=PICTURES)
    unnamed = LineScriptAdapter(pool, script)

    async def scenario() -> tuple[list[StreamEvent], list[StreamEvent]]:
        return await events_of(root, named), await events_of(root, unnamed)

    with_picture, without_picture = drive(scenario())

    assert (with_picture, without_picture) == (
        [FinalEvent(text="Final rc=0", files=(RunFile(name="pic", content=b"p"),))],
        [FinalEvent(text="Final rc=0", files=())],
    )


def test_a_run_that_does_not_end_cleanly_reads_no_picture_folder(
    drive, root: PrivateWorkingRoot, pool: CliProcessPool,
) -> None:
    script = (
        "mkdir -p ../home/generated_images/t; "
        "ln -s /nonexistent ../home/generated_images/t/bad; exit 3\n"
    )
    adapter = LineScriptAdapter(
        pool, script, picture_folders=PICTURES, refuses_failed_ending=True
    )

    async def scenario() -> None:
        with pytest.raises(EndingRefused):
            await events_of(root, adapter)

    drive(scenario())
