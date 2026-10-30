"""What the reservation refuses before it creates the sibling, and the descriptor it owns until it
finishes."""

from __future__ import annotations

import errno
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.support.service_login_support import (  # noqa: F401 - the fixtures are named in their decorators
    A_NEW_DOCUMENT,
    CANARY_REFRESH,
    GOOD_DOCUMENTS,
    LOGIN_NAME,
    Installation,
    Said,
    Stream,
    a_held_installation,
    a_login_installation,
    accepts_anything,
    an_old_file_fails,
    assert_not_written,
    descriptors_closed,
    held_swap,
    run,
    the_signal_handlers_restored,
    what_the_writer_returned,
    what_was_said,
)

import agent_runtime_service.login_file as login_file
from agent_runtime.secret_file import UnsoundSecretFile
from agent_runtime_service.login_file import SwappedDurably, SwapRefused, SwapResult, reserve_swap


def test_a_sibling_swapped_for_a_foreign_file_during_the_read_leaves_the_foreign_file_alone(
    tmp_path: Path,
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    foreign: list[os.stat_result] = []

    def foreign_file_takes_the_sibling(path: Path) -> None:
        path.unlink()
        path.write_bytes(b"foreign")
        foreign.append(os.lstat(path))

    result = reservation.commit(A_NEW_DOCUMENT, foreign_file_takes_the_sibling, replaces=None)

    assert type(result) is SwapRefused
    assert result.sibling_remains is False
    assert os.lstat(installation.sibling) == foreign[0]
    assert installation.sibling.read_bytes() == b"foreign"
    assert installation.unchanged()


def test_a_sibling_taken_by_another_writer_after_our_unlink_is_not_reported_as_ours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    real_unlink = os.unlink

    def unlinking_then_another_writer_reserves(path: Any, *rest: Any) -> None:
        real_unlink(path, *rest)
        Path(path).write_bytes(b"another writer")

    monkeypatch.setattr(os, "unlink", unlinking_then_another_writer_reserves)

    def refusing(path: Path) -> object:
        raise UnsoundSecretFile(f"{path} is not a usable login file")

    result = reservation.commit(A_NEW_DOCUMENT, refusing, replaces=None)

    assert result.sibling_remains is False  # type: ignore[union-attr]
    assert installation.sibling.read_bytes() == b"another writer"


def test_a_descriptor_whose_handoff_fails_is_closed_once_and_the_commit_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    closed = descriptors_closed(monkeypatch)
    open_before = len(os.listdir("/proc/self/fd"))

    def failing(*arguments: object, **keywords: object) -> None:
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    monkeypatch.setattr(login_file, "open", failing, raising=False)

    result = reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=None)

    assert type(result) is SwapRefused
    assert len(closed) == 1
    assert len(os.listdir("/proc/self/fd")) == open_before - 1
    assert installation.directory_holds() == [LOGIN_NAME]


def test_a_reservation_whose_descriptor_cannot_be_inspected_is_refused_and_keeps_its_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    monkeypatch.setattr(login_file, "is_mount_point", lambda path: False)
    created: list[int] = []
    real_open, real_fstat = os.open, os.fstat

    def opening(path: Any, *rest: Any, **keywords: Any) -> int:
        descriptor = real_open(path, *rest, **keywords)
        if Path(path) == installation.sibling:
            created.append(descriptor)
        return descriptor

    def inspecting(descriptor: int) -> os.stat_result:
        if descriptor in created:
            raise OSError(errno.ESTALE, os.strerror(errno.ESTALE))
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "fstat", inspecting)
    closed = descriptors_closed(monkeypatch)

    result = reserve_swap(installation.login)

    assert isinstance(result, SwapRefused)
    assert result.sibling_remains is True
    assert f"{installation.sibling} was created but cannot be inspected" in result.reason
    assert "confirm that no process the service started still runs" in result.reason
    assert closed == created
    assert installation.sibling.exists()


@pytest.mark.parametrize("finish", ["commit", "abandon"])
def test_a_sibling_that_cannot_be_inspected_is_reported_remaining_whatever_the_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    an_old_file_fails(monkeypatch, "lstat", installation.sibling)

    if finish == "commit":
        result = reservation.commit(A_NEW_DOCUMENT, accepts_anything, replaces=None)
    else:
        result = reservation.abandon()

    assert result.sibling_remains is True  # type: ignore[union-attr]
    assert f"{installation.sibling} is not confirmed gone" in result.reason  # type: ignore[union-attr]
    assert installation.unchanged()


def test_an_abandon_whose_close_fails_still_removes_the_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    real_close = os.close

    def closing_then_failing(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "close", closing_then_failing)

    result = reservation.abandon()

    assert result == SwapRefused("abandoned", False)
    assert installation.directory_holds() == [LOGIN_NAME]


def test_a_path_the_operating_system_rejects_outright_is_a_closed_reason(tmp_path: Path) -> None:
    result = reserve_swap(tmp_path / "login\0file")

    assert result == SwapRefused("failed: ValueError", False)


def test_a_second_finisher_during_a_commit_is_refused_and_closes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    reservation = held_swap(installation.login)
    closed = descriptors_closed(monkeypatch)
    inside_the_commit, may_finish = threading.Event(), threading.Event()
    committed: list[SwapResult] = []

    def waiting(path: Path) -> None:
        inside_the_commit.set()
        assert may_finish.wait(timeout=10)

    thread = threading.Thread(
        target=lambda: committed.append(reservation.commit(A_NEW_DOCUMENT, waiting, replaces=None))
    )
    thread.start()
    assert inside_the_commit.wait(timeout=10)
    try:
        with pytest.raises(RuntimeError):
            reservation.abandon()
        assert closed == []
    finally:
        may_finish.set()
        thread.join()

    assert committed == [SwappedDurably()]
    assert installation.login.read_bytes() == A_NEW_DOCUMENT


def test_a_login_file_that_is_a_mount_point_refuses_the_command_naming_the_cure(
    installation: Installation,
    said: Said,
    refusals: list[SwapResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login_file, "is_mount_point", lambda path: True)

    status, heard = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert_not_written(
        installation,
        status,
        heard,
        refusals,
        (CANARY_REFRESH,),
        f"{installation.login} is a mount point, which a rename cannot replace;"
        " bind the directory that holds the file, read-write",
    )


def a_detector_that_fails(error: Exception) -> Callable[[Path], bool]:
    def failing(path: Path) -> bool:
        raise error

    return failing


def test_a_login_file_that_is_a_mount_point_is_refused_before_its_sibling_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    monkeypatch.setattr(login_file, "is_mount_point", lambda path: True)

    result = reserve_swap(installation.login)

    assert type(result) is SwapRefused
    assert result.sibling_remains is False
    assert "bind the directory that holds the file, read-write" in result.reason
    assert installation.directory_holds() == [LOGIN_NAME]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        pytest.param(
            PermissionError(errno.EACCES, os.strerror(errno.EACCES)),
            "cannot be checked for a mount point: Permission denied",
            id="os-error",
        ),
        pytest.param(ValueError(CANARY_REFRESH), "failed: ValueError", id="value-error"),
    ],
)
def test_a_mount_check_that_fails_is_a_closed_refusal_without_a_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    installation = a_held_installation(tmp_path)
    monkeypatch.setattr(login_file, "is_mount_point", a_detector_that_fails(error))

    result = reserve_swap(installation.login)

    assert type(result) is SwapRefused
    assert reason in result.reason
    assert CANARY_REFRESH not in result.reason
    assert result.sibling_remains is False
    assert installation.directory_holds() == [LOGIN_NAME]


def test_the_real_check_tells_a_mount_point_from_a_file_and_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.write_bytes(b"")
    (tmp_path / "link").symlink_to("/proc")

    assert [
        login_file.is_mount_point(path)
        for path in (
            Path("/proc"),
            plain,
            tmp_path / "missing",
            tmp_path / "link" / "version",
        )
    ] == [True, False, False, False]


def test_without_descriptor_information_the_device_comparison_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_file, "_DESCRIPTOR_INFORMATION", tmp_path / "absent")

    assert [login_file.is_mount_point(Path("/proc")), login_file.is_mount_point(tmp_path)] == [
        True,
        False,
    ]


def test_the_device_comparison_follows_the_directory_and_tells_the_root_from_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_file, "_DESCRIPTOR_INFORMATION", tmp_path / "absent")
    (tmp_path / "link").symlink_to("/proc")

    assert [
        login_file.is_mount_point(path) for path in (Path("/"), tmp_path / "link" / "version")
    ] == [
        True,
        False,
    ]


def test_the_device_comparison_raises_for_a_login_below_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_file, "_DESCRIPTOR_INFORMATION", tmp_path / "absent")
    (tmp_path / "plain").write_bytes(b"")

    with pytest.raises(NotADirectoryError):
        login_file.is_mount_point(tmp_path / "plain" / "login")


def test_a_missing_descriptor_entry_is_a_closed_refusal_without_a_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(login_file, "_DESCRIPTOR_INFORMATION", tmp_path / "empty")

    result = reserve_swap(installation.login)

    assert type(result) is SwapRefused
    assert "cannot be checked for a mount point" in result.reason
    assert result.sibling_remains is False
    assert installation.directory_holds() == [LOGIN_NAME]


def test_descriptor_information_that_cannot_be_looked_up_is_a_refusal_not_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = a_held_installation(tmp_path)
    (tmp_path / "plain").write_bytes(b"")
    monkeypatch.setattr(login_file, "_DESCRIPTOR_INFORMATION", tmp_path / "plain" / "fdinfo")

    result = reserve_swap(installation.login)

    assert type(result) is SwapRefused
    assert "cannot be checked for a mount point: Not a directory" in result.reason
    assert installation.directory_holds() == [LOGIN_NAME]


def test_the_device_comparison_takes_a_missing_login_for_no_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_file, "_DESCRIPTOR_INFORMATION", tmp_path / "absent")

    assert login_file.is_mount_point(tmp_path / "missing") is False


def test_the_mount_id_is_read_from_descriptor_information_that_holds_it() -> None:
    text = "pos:\t0\nflags:\t010000000\nmnt_id:\t42\nino:\t1234\n"

    assert login_file.mount_identifier(text) == "42"


def test_descriptor_information_without_a_mount_id_cannot_tell() -> None:
    with pytest.raises(OSError, match="no mount id"):
        login_file.mount_identifier("pos:\t0\nflags:\t010000000\nino:\t1234\n")
