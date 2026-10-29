"""The reader that does not follow links returns bytes or one closed fault and never raises."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from agent_runtime.secret_file import (
    MAX_SECRET_FILE_BYTES,
    SecretFileFault,
    read_secret_file_without_following_links,
)

CONTENT = b'{"canary": "document"}'


def test_a_regular_file_is_read_whatever_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(CONTENT)
    path.chmod(0o644)

    assert read_secret_file_without_following_links(path) == CONTENT


def test_a_missing_file_is_missing(tmp_path: Path) -> None:
    assert read_secret_file_without_following_links(tmp_path / "gone") is SecretFileFault.MISSING


def test_a_symbolic_link_is_refused_and_its_target_is_not_read(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(CONTENT)
    link = tmp_path / "auth.json"
    link.symlink_to(target)

    assert read_secret_file_without_following_links(link) is SecretFileFault.LINK


def _make_socket(path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(path.name)


@pytest.mark.parametrize(
    "make", [os.mkfifo, os.mkdir, _make_socket], ids=["fifo", "directory", "socket"]
)
def test_a_file_that_is_not_regular_is_refused_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "auth.json"
    make(path)

    assert read_secret_file_without_following_links(path) is SecretFileFault.NOT_REGULAR


@pytest.mark.parametrize(
    ("size", "fits"), [(MAX_SECRET_FILE_BYTES, True), (MAX_SECRET_FILE_BYTES + 1, False)]
)
def test_the_file_may_hold_at_most_the_secret_file_bound(
    tmp_path: Path, size: int, fits: bool
) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(b"x" * size)

    read = read_secret_file_without_following_links(path)

    assert (read == b"x" * size) if fits else (read is SecretFileFault.TOO_LARGE)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_file_that_cannot_be_opened_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(CONTENT)
    path.chmod(0)

    assert read_secret_file_without_following_links(path) is SecretFileFault.UNREADABLE


def test_a_path_with_a_null_byte_is_unreadable_not_raised(tmp_path: Path) -> None:
    assert (
        read_secret_file_without_following_links(tmp_path / "auth\0.json")
        is SecretFileFault.UNREADABLE
    )


@pytest.mark.parametrize("failing_call", ["fstat", "read", "close"])
def test_a_failure_of_any_descriptor_call_is_unreadable_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_call: str
) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(CONTENT)
    real_call = getattr(os, failing_call)
    attempts: list[int] = []

    def failing(descriptor: int, *arguments: int) -> object:
        attempts.append(descriptor)
        if failing_call == "close":
            real_call(descriptor)
        raise OSError(5, "injected")

    monkeypatch.setattr(os, failing_call, failing)

    assert read_secret_file_without_following_links(path) is SecretFileFault.UNREADABLE
    if failing_call == "close":
        assert len(attempts) == 1
