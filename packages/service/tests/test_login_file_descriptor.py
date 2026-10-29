"""The reservation owns its descriptor until it finishes: what is closed, when, and by whom."""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from tests.support.service_login_support import (
    A_NEW_DOCUMENT,
    LOGIN_NAME,
    a_held_installation,
    accepts_anything,
    an_old_file_fails,
    descriptors_closed,
    held_swap,
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
