"""Read one run's output folder after its group is gone, never following a link.

``collect_output`` opens the run's tree, its ``work`` directory and its ``output`` folder
each relative to the one before, so no component the CLI could have replaced is followed.
Every entry must be a regular file with one name; it is read through its own descriptor, at
most one byte past the limit in all. The read is synchronous and bounded, and it names no
provider. A refusal carries one closed :class:`RunOutputCause` and no name, path or content.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from contextlib import ExitStack
from enum import StrEnum
from typing import Final, NoReturn

from pydantic import ValidationError

from agent_runtime.errors import raise_without_chain
from agent_runtime.process import RunDirectories
from agent_runtime_client.wire import RunFile

log = logging.getLogger(__name__)

_FOLDER_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_ENTRY_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY
_FOLDER_REPLACED_ERRORS: Final = frozenset({errno.ENOENT, errno.ELOOP, errno.ENOTDIR})
_READ_CHUNK_BYTES: Final = 64 * 1024


class RunOutputCause(StrEnum):
    FOLDER_REPLACED = "the output folder is missing, a link or not a directory"
    LINK = "an output entry that is a symbolic link"
    NOT_A_REGULAR_FILE = "an output entry that is not a regular file"
    SECOND_NAME = "an output file that has a second name"
    NAME_REFUSED = "an output file name the wire refuses"
    OVER_LIMIT = "output files over the limit"
    UNREADABLE = "an output entry that could not be read"


class RunOutputRefused(Exception):
    """A run's output was refused; it carries only the cause."""

    def __init__(self, cause: RunOutputCause) -> None:
        super().__init__(f"The run's output was refused: {cause.value}.")
        self.cause = cause


def _refuse(cause: RunOutputCause) -> NoReturn:
    log.warning("A run's output was refused (%s)", cause.name)
    raise_without_chain(RunOutputRefused(cause))


def collect_output(directories: RunDirectories, max_files_bytes: int) -> tuple[RunFile, ...]:
    """Every file of the run's output folder in name order; at most ``max_files_bytes`` in all."""
    with ExitStack() as descriptors:
        tree = _open_folder(directories.working_directory.parent, None, descriptors)
        work = _open_folder(directories.working_directory.name, tree, descriptors)
        output = _open_folder(directories.output.name, work, descriptors)
        try:
            names = sorted(os.listdir(output))
        except OSError:
            _refuse(RunOutputCause.UNREADABLE)
        files = []
        remaining = max_files_bytes
        for name in names:
            content = _read_file(name, output, remaining + 1)
            if len(content) > remaining:
                _refuse(RunOutputCause.OVER_LIMIT)
            remaining -= len(content)
            files.append(_file_of(name, content))
        return tuple(files)


def _open_folder(path: object, parent: int | None, descriptors: ExitStack) -> int:
    try:
        descriptor = os.open(path, _FOLDER_FLAGS, dir_fd=parent)
    except OSError as error:
        if error.errno in _FOLDER_REPLACED_ERRORS:
            _refuse(RunOutputCause.FOLDER_REPLACED)
        _refuse(RunOutputCause.UNREADABLE)
    descriptors.callback(os.close, descriptor)
    return descriptor


def _read_file(name: str, folder: int, limit_bytes: int) -> bytes:
    try:
        descriptor = os.open(name, _ENTRY_FLAGS, dir_fd=folder)
    except OSError as error:
        _refuse(_cause_of_open_failure(error))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            _refuse(RunOutputCause.NOT_A_REGULAR_FILE)
        if info.st_nlink != 1:
            _refuse(RunOutputCause.SECOND_NAME)
        return _read_up_to(descriptor, limit_bytes)
    except OSError:
        _refuse(RunOutputCause.UNREADABLE)
    finally:
        os.close(descriptor)


def _cause_of_open_failure(error: OSError) -> RunOutputCause:
    if error.errno == errno.ELOOP:
        return RunOutputCause.LINK
    if error.errno == errno.ENXIO:
        return RunOutputCause.NOT_A_REGULAR_FILE
    return RunOutputCause.UNREADABLE


def _read_up_to(descriptor: int, limit_bytes: int) -> bytes:
    content = bytearray()
    while len(content) < limit_bytes:
        chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, limit_bytes - len(content)))
        if not chunk:
            break
        content += chunk
    return bytes(content)


def _file_of(name: str, content: bytes) -> RunFile:
    try:
        return RunFile(name=name, content=content)
    except ValidationError:
        _refuse(RunOutputCause.NAME_REFUSED)
