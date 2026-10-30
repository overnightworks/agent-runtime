"""The login writer through the command: ``swap_in_login_file`` swaps a checked document in beside
the old file and removes its own sibling, or says why not.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from tests.support.provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    SAMPLE_CLAUDE_SETUP_TOKEN,
)
from tests.support.service_login_support import (  # noqa: F401 - the fixtures are named in their decorators
    A_NEW_DOCUMENT,
    CANARY_FOREIGN,
    CANARY_REFRESH,
    CODEX_FILE_REFUSAL,
    GOOD_DOCUMENTS,
    LOGIN_NAME,
    Installation,
    Said,
    Stream,
    a_held_installation,
    a_login_installation,
    accepts_anything,
    an_installation,
    an_old_file_fails,
    assert_not_written,
    descriptors_closed,
    held_swap,
    line_of,
    reader_hook,
    run,
    send,
    send_while_blocked,
    the_signal_handlers_restored,
    the_signals_received,
    what_the_writer_returned,
    what_was_said,
)
from tests.support.service_support import reaches_text

import agent_runtime_service.config as service_config
import agent_runtime_service.login_file as login_file
from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES, UnsoundSecretFile, read_secret_file
from agent_runtime_service.config import Provider
from agent_runtime_service.login import EXIT_REMAINS
from agent_runtime_service.login_file import (
    ReservedSwap,
    SiblingHeld,
    SourceChanged,
    SwappedDurabilityUnconfirmed,
    SwappedDurably,
    SwapRefused,
    SwapResult,
    reserve_swap,
    swap_in_login_file,
)

# The good path.


def test_a_login_is_written_exactly_and_reported_on_descriptor_two_only(
    installation: Installation, said: Said
) -> None:
    document = GOOD_DOCUMENTS[installation.provider]

    status, heard = run(installation, Stream(document), said)

    assert (status, installation.login.read_bytes()) == (0, document)
    assert stat.S_IMODE(installation.login.stat().st_mode) == 0o600
    assert installation.directory_holds() == [LOGIN_NAME]
    assert heard == line_of(installation, "written") + "\n"
    assert said.stderr == ""
    assert not any(word in heard for word in (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN, CANARY_REFRESH))


def test_a_usable_file_is_replaced_by_one_rename_and_the_reader_reads_the_new_document(
    installation: Installation, said: Said
) -> None:
    status, _ = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    reader = service_config.LOGIN_READERS[installation.provider]
    assert status == 0
    assert installation.login.stat().st_ino != installation.old_inode
    assert reader(installation.login) is not None
    assert installation.login.read_bytes() != installation.old_bytes


# Not written. The group invariant is `assert_not_written`.


REFUSED_DOCUMENTS = [
    pytest.param(Provider.OPENAI, b"", (), "is not a Codex login document", id="codex-empty"),
    pytest.param(
        Provider.OPENAI,
        json.dumps({"OPENAI_API_KEY": CANARY_FOREIGN}).encode(),
        (CANARY_FOREIGN,),
        CODEX_FILE_REFUSAL,
        id="codex-api-key",
    ),
    pytest.param(
        Provider.OPENAI,
        b"not json " + CANARY_FOREIGN.encode(),
        (CANARY_FOREIGN,),
        "is not a Codex login document",
        id="codex-not-json",
    ),
    pytest.param(
        Provider.OPENAI,
        b"x" * (MAX_SECRET_FILE_BYTES + 1000),
        (),
        f"is larger than {MAX_SECRET_FILE_BYTES} bytes",
        id="codex-too-large",
    ),
    pytest.param(
        Provider.OPENAI,
        SAMPLE_CLAUDE_SETUP_TOKEN.encode(),
        (SAMPLE_CLAUDE_SETUP_TOKEN,),
        "is not a Codex login document",
        id="codex-given-a-claude-token",
    ),
    pytest.param(
        Provider.ANTHROPIC,
        b'{"claudeAiOauth": {"accessToken": "' + CANARY_FOREIGN.encode() + b'"}}',
        (CANARY_FOREIGN, "claudeAiOauth"),
        "holds a JSON document",
        id="claude-credentials-file",
    ),
    pytest.param(
        Provider.ANTHROPIC,
        b"sk-ant-api03-" + CANARY_FOREIGN.encode() + b"\n",
        (CANARY_FOREIGN,),
        "holds an API key",
        id="claude-api-key",
    ),
    pytest.param(
        Provider.ANTHROPIC, b" new-token\n", (), "holds whitespace", id="claude-leading-space"
    ),
]


@pytest.mark.parametrize(("provider", "document", "canaries", "reason"), REFUSED_DOCUMENTS)
def test_a_document_the_tables_reader_refuses_is_not_written_and_the_old_file_stays(
    tmp_path: Path,
    said: Said,
    refusals: list[SwapResult],
    provider: Provider,
    document: bytes,
    canaries: tuple[str, ...],
    reason: str,
) -> None:
    installation = an_installation(tmp_path, provider)
    stream = Stream(document)

    status, heard = run(installation, stream, said)

    assert_not_written(installation, status, heard, refusals, canaries, reason)
    assert f"the new document for {installation.login} was refused" in heard
    assert str(installation.sibling) not in heard
    assert stream.tell() == min(len(document), MAX_SECRET_FILE_BYTES + 1)


def test_a_login_directory_that_cannot_be_written_refuses_the_create(
    installation: Installation, said: Said, refusals: list[SwapResult]
) -> None:
    installation.login.parent.chmod(0o500)
    try:
        status, heard = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)
    finally:
        installation.login.parent.chmod(0o700)

    assert_not_written(
        installation, status, heard, refusals, (CANARY_REFRESH,), f"{installation.sibling}"
    )
    assert "cannot be created: Permission denied" in heard


def test_a_full_disk_while_writing_removes_the_sibling_and_names_the_cause(
    installation: Installation,
    said: Said,
    refusals: list[SwapResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FullDisk:
        def __init__(self, descriptor: int, mode: str, *, closefd: bool) -> None:
            self.file = open(descriptor, mode, closefd=closefd)  # noqa: SIM115 - closed by __exit__

        def __enter__(self) -> FullDisk:
            return self

        def __exit__(self, *exception: object) -> None:
            self.file.close()

        def write(self, content: bytes) -> None:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(login_file, "open", FullDisk, raising=False)

    status, heard = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert_not_written(
        installation,
        status,
        heard,
        refusals,
        (CANARY_REFRESH,),
        f"{installation.login} cannot be swapped in: No space left on device",
    )


def test_a_path_the_rename_cannot_replace_removes_the_sibling_and_leaves_the_directory(
    tmp_path: Path, said: Said, refusals: list[SwapResult]
) -> None:
    installation = an_installation(tmp_path, Provider.OPENAI, old=False)
    installation.login.mkdir()
    (installation.login / "kept").write_text("kept")

    status, heard = run(installation, Stream(GOOD_DOCUMENTS[Provider.OPENAI]), said)

    assert status == 1
    assert installation.directory_holds() == [LOGIN_NAME]
    assert (installation.login / "kept").read_text() == "kept"
    assert f"{installation.login} cannot be swapped in:" in heard
    assert CANARY_REFRESH not in heard
    assert not reaches_text(refusals, (CANARY_REFRESH,))


def a_frame_that_holds_the_document_raises(
    where: str, installation: Installation, monkeypatch: pytest.MonkeyPatch
) -> Stream:
    def raising() -> None:
        error = RuntimeError(CANARY_FOREIGN)
        error.detail = CANARY_FOREIGN  # type: ignore[attr-defined]
        raise error

    if where == "read":
        return Stream(GOOD_DOCUMENTS[installation.provider], raising)
    reader_hook(monkeypatch, installation.provider, raising)
    return Stream(GOOD_DOCUMENTS[installation.provider])


@pytest.mark.parametrize("where", ["read", "reader"])
def test_an_unexpected_exception_in_a_frame_that_holds_the_document_is_a_closed_reason(
    installation: Installation,
    said: Said,
    refusals: list[SwapResult],
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    stream = a_frame_that_holds_the_document_raises(where, installation, monkeypatch)

    status, heard = run(installation, stream, said)

    assert_not_written(
        installation,
        status,
        heard,
        refusals,
        (CANARY_FOREIGN, CANARY_REFRESH),
        "failed: RuntimeError",
    )


def test_a_sibling_that_appears_after_the_check_is_refused_and_left_untouched(
    installation: Installation, said: Said
) -> None:
    def foreign_sibling_appears() -> None:
        installation.sibling.write_bytes(b"foreign")

    status, heard = run(
        installation,
        Stream(GOOD_DOCUMENTS[installation.provider], foreign_sibling_appears),
        said,
    )

    assert status == EXIT_REMAINS
    assert (
        line_of(
            installation, f"not written: {login_file.held_sibling_sentence(installation.sibling)}"
        )
        in heard
    )
    assert installation.unchanged()
    assert installation.sibling.read_bytes() == b"foreign"


def a_sibling_not_confirmed_gone(monkeypatch: pytest.MonkeyPatch, sibling: Path, how: str) -> None:
    """The writer's unlink and lstat of ``sibling`` misbehave as ``how`` says.

    ``unlink-raises`` and ``unlink-does-nothing`` leave the file for a truthful lstat to find;
    ``lstat-denied`` lets the unlink succeed and then refuses every inspection; ``both`` fails
    both. The check before the read still passes, because no unlink has happened yet.
    """
    real_unlink, real_lstat = os.unlink, os.lstat
    unlinks: list[Path] = []

    def unlinking(path: Any, *rest: Any) -> None:
        if Path(path) == sibling:
            unlinks.append(sibling)
            if how in ("unlink-raises", "both"):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
            if how == "unlink-does-nothing":
                return
        real_unlink(path, *rest)

    def inspecting(path: Any, *rest: Any) -> os.stat_result:
        if Path(path) == sibling and unlinks and how in ("lstat-denied", "both"):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real_lstat(path, *rest)

    monkeypatch.setattr(os, "unlink", unlinking)
    monkeypatch.setattr(os, "lstat", inspecting)


@pytest.mark.parametrize("how", ["unlink-raises", "unlink-does-nothing", "lstat-denied", "both"])
def test_a_sibling_that_cannot_be_confirmed_gone_ends_with_exit_three_naming_it(
    tmp_path: Path, said: Said, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    installation = an_installation(tmp_path, Provider.OPENAI)
    a_sibling_not_confirmed_gone(monkeypatch, installation.sibling, how)

    status, heard = run(installation, Stream(b""), said)

    assert status == 3
    assert installation.unchanged()
    assert f"{installation.sibling} is not confirmed gone and may hold all or part" in heard
    assert "not written" in heard


# Atomic replacement.


def test_the_old_file_is_whole_until_the_rename_replaces_it(
    installation: Installation, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    before_rename: list[tuple[bytes, int]] = []

    def replacing(source: Any, destination: Any) -> None:
        before_rename.append((installation.login.read_bytes(), installation.login.stat().st_ino))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replacing)

    status, _ = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert status == 0
    assert before_rename == [(installation.old_bytes, installation.old_inode)]


def test_a_rename_that_fails_leaves_the_old_file_with_its_bytes_and_inode(
    installation: Installation,
    said: Said,
    refusals: list[SwapResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(source: Any, destination: Any) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "replace", failing)

    status, heard = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert_not_written(
        installation, status, heard, refusals, (CANARY_REFRESH,), "Input/output error"
    )


# A path with no file name has no sibling to swap through.


def test_the_writer_refuses_a_path_without_a_file_name_as_a_closed_result() -> None:
    document = GOOD_DOCUMENTS[Provider.OPENAI]

    refused = swap_in_login_file(Path("/"), document, lambda path: None)

    assert refused == SwapRefused(refused.reason, False)
    assert "has no file name" in refused.reason
    assert not reaches_text(refused, (CANARY_REFRESH,))


# Durability.


def the_syncs_recorded(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Appends ``file-sync``, ``rename`` and ``directory-sync`` in the order they happen."""
    real_fsync, real_replace = os.fsync, os.replace

    def syncing(descriptor: int) -> None:
        events.append(
            "directory-sync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file-sync"
        )
        real_fsync(descriptor)

    def replacing(source: Any, destination: Any) -> None:
        events.append("rename")
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", syncing)
    monkeypatch.setattr(os, "replace", replacing)


def a_sync_that_fails(monkeypatch: pytest.MonkeyPatch, *, directory: bool) -> None:
    """``os.fsync`` raises ``EIO`` for a directory or for a file, and syncs the other kind."""
    real_fsync = os.fsync

    def syncing(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) is directory:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", syncing)


def a_directory_that_cannot_be_opened(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    real_open = os.open

    def opening(path: Any, flags: int, *rest: Any) -> int:
        if Path(path) == directory and flags & os.O_DIRECTORY and not flags & os.O_PATH:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real_open(path, flags, *rest)

    monkeypatch.setattr(os, "open", opening)


def test_the_file_is_synced_before_the_rename_and_the_directory_after_it(
    installation: Installation, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    the_syncs_recorded(monkeypatch, events)

    status, _ = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert (status, events) == (0, ["file-sync", "rename", "directory-sync"])


def test_a_login_into_an_absent_file_is_written_and_leaves_no_sibling(
    tmp_path: Path, said: Said
) -> None:
    installation = an_installation(tmp_path, Provider.ANTHROPIC, old=False)
    document = GOOD_DOCUMENTS[Provider.ANTHROPIC]

    status, heard = run(installation, Stream(document), said)

    assert (status, installation.login.read_bytes()) == (0, document)
    assert stat.S_IMODE(installation.login.stat().st_mode) == 0o600
    assert installation.directory_holds() == [LOGIN_NAME]
    assert heard == line_of(installation, "written") + "\n"


@pytest.mark.parametrize("failure", ["sync", "open"])
def test_a_directory_that_cannot_be_synced_after_the_rename_is_written_with_the_qualification(
    installation: Installation,
    said: Said,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "sync":
        a_sync_that_fails(monkeypatch, directory=True)
    else:
        a_directory_that_cannot_be_opened(monkeypatch, installation.login.parent)
    document = GOOD_DOCUMENTS[installation.provider]

    status, heard = run(installation, Stream(document), said)

    assert (status, installation.login.read_bytes()) == (0, document)
    assert installation.directory_holds() == [LOGIN_NAME]
    assert heard.startswith(line_of(installation, "written; durability not confirmed: "))
    assert CANARY_REFRESH not in heard


def test_a_file_that_cannot_be_synced_is_not_swapped_in_and_the_old_file_stays(
    installation: Installation,
    said: Said,
    refusals: list[SwapResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a_sync_that_fails(monkeypatch, directory=False)

    status, heard = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert_not_written(
        installation,
        status,
        heard,
        refusals,
        (CANARY_REFRESH,),
        f"{installation.login} cannot be swapped in: Input/output error",
    )


# A holder of the reservation.


def test_a_commit_that_replaces_the_document_it_renewed_swaps_it_in_durably(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)

    result = reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=installation.old_bytes)

    assert result == SwappedDurably()
    assert installation.login.read_bytes() == A_NEW_DOCUMENT
    assert installation.directory_holds() == [LOGIN_NAME]


def test_a_commit_for_a_source_that_changed_refuses_and_keeps_the_newer_source(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    installation.login.write_bytes(b"newer\n")
    before = installation.snapshot()

    result = reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=installation.old_bytes)

    assert isinstance(result, SourceChanged)
    assert result.sibling_remains is False
    assert installation.snapshot() == (b"newer\n", before[1], [LOGIN_NAME])
    assert not reaches_text(result, (CANARY_REFRESH, "newer"))
    with pytest.raises(RuntimeError):
        reservation.abandon()


@pytest.mark.parametrize("unreadable", ["gone", "mode-0644", "directory"])
def test_a_commit_for_a_source_that_is_gone_or_cannot_be_read_refuses_and_leaves_it_as_it_is(
    tmp_path: Path, unreadable: str
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    if unreadable == "mode-0644":
        installation.login.chmod(0o644)
    else:
        installation.login.unlink()
    if unreadable == "directory":
        installation.login.mkdir()
    status = None if unreadable == "gone" else os.lstat(installation.login)

    result = reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=installation.old_bytes)

    assert isinstance(result, SourceChanged)
    assert installation.directory_holds() == ([] if unreadable == "gone" else [LOGIN_NAME])
    assert (None if unreadable == "gone" else os.lstat(installation.login)) == status
    assert not reaches_text(result, (CANARY_REFRESH,))


def test_an_unconditional_commit_replaces_a_source_that_changed(tmp_path: Path) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    installation.login.write_bytes(b"newer\n")

    result = reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=None)

    assert result == SwappedDurably()
    assert installation.login.read_bytes() == A_NEW_DOCUMENT


def test_a_commit_whose_reader_refuses_leaves_the_source_and_removes_the_sibling(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)

    def refusing(path: Path) -> object:
        raise UnsoundSecretFile(f"{path} is not a usable login file")

    result = reservation.commit(A_NEW_DOCUMENT, refusing, replaces=None)

    assert type(result) is SwapRefused
    assert result.sibling_remains is False
    assert str(installation.login) in result.reason
    assert not reaches_text(result, (CANARY_FOREIGN, CANARY_REFRESH))
    assert installation.unchanged()
    assert installation.directory_holds() == [LOGIN_NAME]


def test_a_commit_whose_directory_cannot_be_synced_keeps_the_new_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    a_sync_that_fails(monkeypatch, directory=True)

    result = reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=None)

    assert isinstance(result, SwappedDurabilityUnconfirmed)
    assert str(installation.login.parent) in result.reason
    assert not reaches_text(result, (CANARY_REFRESH,))
    assert installation.login.read_bytes() == A_NEW_DOCUMENT


def test_an_abandoned_reservation_removes_its_sibling_and_leaves_the_source(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)

    result = reservation.abandon()

    assert result == SwapRefused("abandoned", False)
    assert not reaches_text(result, (CANARY_REFRESH,))
    assert installation.unchanged()
    assert installation.directory_holds() == [LOGIN_NAME]


def test_an_abandon_whose_unlink_fails_reports_the_sibling_remaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    an_old_file_fails(monkeypatch, "unlink", installation.sibling)

    result = reservation.abandon()

    assert result.sibling_remains is True
    assert f"{installation.sibling} is not confirmed gone" in result.reason
    assert not reaches_text(result, (CANARY_REFRESH,))
    assert installation.unchanged()


def test_a_second_reservation_meets_the_holders_sibling_and_leaves_it_untouched(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    holder = held_swap(installation.login)
    held = os.lstat(installation.sibling)

    second = reserve_swap(installation.login)

    assert isinstance(second, SiblingHeld)
    assert second.sibling_remains is False
    assert second.reason == login_file.held_sibling_sentence(installation.sibling)
    assert "earlier install" not in second.reason
    assert not reaches_text(second, (CANARY_REFRESH,))
    assert os.lstat(installation.sibling) == held
    assert holder.commit(A_NEW_DOCUMENT, accepts_anything, replaces=None) == SwappedDurably()


def test_a_reservation_in_a_directory_that_cannot_be_written_is_not_a_held_sibling(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    installation.login.parent.chmod(0o500)
    try:
        result = reserve_swap(installation.login)
    finally:
        installation.login.parent.chmod(0o700)

    assert type(result) is SwapRefused
    assert f"{installation.sibling} cannot be created: Permission denied" in result.reason


def reserved_in_another_thread(login_path: Path) -> ReservedSwap:
    holder: list[ReservedSwap] = []
    thread = threading.Thread(target=lambda: holder.append(held_swap(login_path)))
    thread.start()
    thread.join()
    return holder[0]


def test_a_reservation_made_in_one_thread_is_committed_in_another_and_readers_see_whole_files(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = reserved_in_another_thread(installation.login)
    during = read_secret_file(installation.login, "login file")
    committed: list[SwapResult] = []
    thread = threading.Thread(
        target=lambda: committed.append(
            reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=during)
        )
    )

    thread.start()
    thread.join()

    assert during == installation.old_bytes
    assert committed == [SwappedDurably()]
    assert read_secret_file(installation.login, "login file") == A_NEW_DOCUMENT


RESERVE_AND_EXIT = (
    "import sys; from pathlib import Path;"
    " from agent_runtime_service.login_file import SwapRefused, reserve_swap;"
    " held = reserve_swap(Path(sys.argv[1])); sys.exit(isinstance(held, SwapRefused))"
)


def test_a_reservation_left_by_an_exited_process_keeps_the_sibling_and_holds_later_writers_out(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)

    finished = subprocess.run(
        [sys.executable, "-c", RESERVE_AND_EXIT, str(installation.login)], check=False
    )

    assert finished.returncode == 0
    assert installation.sibling.read_bytes() == b""
    assert stat.S_IMODE(installation.sibling.stat().st_mode) == 0o600
    assert isinstance(reserve_swap(installation.login), SiblingHeld)


def test_a_sibling_replaced_by_another_file_during_the_hold_is_refused_and_left_untouched(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    installation.sibling.unlink()
    installation.sibling.write_bytes(b"foreign")
    foreign = os.lstat(installation.sibling)

    result = reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=None)

    assert type(result) is SwapRefused
    assert result.sibling_remains is False
    assert os.lstat(installation.sibling) == foreign
    assert installation.sibling.read_bytes() == b"foreign"
    assert installation.unchanged() is True
    assert not reaches_text(result, (CANARY_REFRESH,))


@pytest.mark.parametrize("second", ["commit", "abandon"])
@pytest.mark.parametrize("first", ["commit", "abandon"])
def test_a_finished_reservation_refuses_any_second_use_without_closing_anything_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str, second: str
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)

    def finish(how: str) -> object:
        if how == "abandon":
            return reservation.abandon()
        return reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=None)

    finish(first)
    after_first = installation.snapshot()
    closed = descriptors_closed(monkeypatch)

    with pytest.raises(RuntimeError):
        finish(second)

    assert closed == []
    assert installation.snapshot() == after_first


LIST_OPEN_FILES = (
    "import os\n"
    "for name in os.listdir('/proc/self/fd'):\n"
    "    try:\n"
    "        print(os.readlink('/proc/self/fd/' + name))\n"
    "    except OSError:\n"
    "        pass\n"
)


def test_a_child_started_during_the_hold_has_no_descriptor_to_the_sibling(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)

    own: list[str] = []
    for name in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            own.append(os.readlink(f"/proc/self/fd/{name}"))
    child = subprocess.run(  # inherits every descriptor that is not close-on-exec
        [sys.executable, "-c", LIST_OPEN_FILES],
        capture_output=True,
        text=True,
        close_fds=False,
        check=False,
    )

    assert str(installation.sibling) in own
    assert str(installation.sibling) not in child.stdout.splitlines()
    reservation.abandon()
