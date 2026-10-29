"""The one writer of a login file: a new document swapped in beside the old one.

A writer first reserves the sibling of the destination (``rename(2)`` refuses across mount
points) with an exclusive create, then writes the document to it and syncs it. The provider's own
reader reads it back on that very file, and only then is it renamed over the old file and the
directory synced. A failure before the rename leaves the old file byte for byte. A reservation
that is never finished keeps its sibling on disk, which keeps every other writer out.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_runtime.secret_file import UnsoundSecretFile, read_secret_file, shown_text

LOGIN_FILE_MODE: Final = 0o600


def swap_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.new")


_RECOVERY = (
    "stop the service and any other login command, confirm that no process the service started"
    " still runs, then remove it and run the command again"
)


def held_sibling_sentence(sibling: Path) -> str:
    """What the operator is told about a sibling that exists: it may belong to a running writer."""
    return (
        f"{shown_text(str(sibling))} may hold a login and may belong to a writer that still runs"
        f" or was interrupted; {_RECOVERY}"
    )


@dataclass(frozen=True)
class SwapRefused:
    """Why nothing was swapped in: a closed reason, and whether the writer's sibling remains."""

    reason: str
    sibling_remains: bool


@dataclass(frozen=True)
class SiblingHeld(SwapRefused):
    """The exclusive create met an existing sibling, which is not this writer's."""


@dataclass(frozen=True)
class SourceChanged(SwapRefused):
    """The source no longer holds the document the swap was to replace."""


@dataclass(frozen=True)
class SwappedDurably:
    """Renamed, then the directory was synced."""


@dataclass(frozen=True)
class SwappedDurabilityUnconfirmed:
    """Renamed, but the directory sync failed: the new document is in place and stays."""

    reason: str


SwapResult = SwappedDurably | SwappedDurabilityUnconfirmed | SwapRefused


def swap_sibling_or_refusal(path: Path) -> Path | SwapRefused:
    """The sibling beside ``path``, or the refusal when ``path`` has no file name to derive it."""
    try:
        return swap_sibling(path)
    except ValueError:
        return SwapRefused(f"{shown_text(str(path))} has no file name to swap in beside", False)


Identity = tuple[int, int]


class ReservedSwap:
    """The sibling of a destination, held open until ``commit`` or ``abandon`` finishes it.

    Built only by ``reserve_swap``. It holds no content, and any one thread may finish it,
    exactly once; a second use raises ``RuntimeError``. It owns its descriptor until it finishes,
    so the inode stays allocated and the identity guard can tell its own sibling from a foreign
    file created later. It has no finaliser on purpose: an unfinished reservation leaves its
    sibling on disk, and process exit closes the descriptor.
    """

    def __init__(self, destination: Path, sibling: Path, descriptor: int, identity: Identity):
        self._destination = destination
        self._sibling = sibling
        self._descriptor = descriptor
        self._identity = identity
        self._finishing = threading.Lock()

    @property
    def destination(self) -> Path:
        return self._destination

    @property
    def sibling(self) -> Path:
        return self._sibling

    def commit(
        self, content: bytes, reader: Callable[[Path], object], *, replaces: bytes | None
    ) -> SwapResult:
        """Write, sync, read back, rename over the destination, sync the directory.

        With ``replaces`` the rename happens only while the destination still holds those bytes.
        A second use raises ``RuntimeError``; otherwise it never raises an ``Exception``: every
        refusal is a closed reason that holds no part of ``content``. The caller must have no
        asynchronous exception arising in this thread: the login command disregards interrupts,
        and a worker thread never receives a signal.
        """
        self._finish()
        try:
            refusal = self._refusal_before_rename(content, reader, replaces)
            if refusal is not None:
                return self._with_sibling_removed(refusal)
        finally:
            _close_quietly(self._descriptor)
        return _directory_synced(self._destination.parent)

    def abandon(self) -> SwapRefused:
        """Release the reservation without writing: remove the sibling, then close the file."""
        self._finish()
        try:
            return self._with_sibling_removed(SwapRefused("abandoned", False))
        finally:
            _close_quietly(self._descriptor)

    def _finish(self) -> None:
        if not self._finishing.acquire(blocking=False):
            raise RuntimeError("the reservation is finished")

    def _refusal_before_rename(
        self, content: bytes, reader: Callable[[Path], object], replaces: bytes | None
    ) -> SwapRefused | None:
        try:
            with open(self._descriptor, "wb", closefd=False) as new_file:
                new_file.write(content)
                new_file.flush()
                os.fsync(new_file.fileno())
            reader(self._sibling)
            if replaces is not None and not _holds(self._destination, replaces):
                return SourceChanged(
                    f"{shown_text(str(self._destination))} no longer holds the document"
                    " this swap was to replace",
                    False,
                )
            if _identity(os.lstat(self._sibling)) != self._identity:
                return SwapRefused(
                    f"{shown_text(str(self._sibling))} is no longer the file this writer created",
                    False,
                )
            os.replace(self._sibling, self._destination)
        except UnsoundSecretFile as refusal:
            return SwapRefused(
                _refusal_about_the_login_file(self._destination, self._sibling, str(refusal)),
                False,
            )
        except OSError as error:
            return SwapRefused(
                f"{shown_text(str(self._destination))} cannot be swapped in: {error.strerror}",
                False,
            )
        except Exception as error:
            return SwapRefused(f"failed: {type(error).__name__}", False)
        return None

    def _with_sibling_removed(self, refusal: SwapRefused) -> SwapRefused:
        """Unlinks the writer's own sibling and reports it unless ``lstat`` shows it gone.

        A sibling that is no longer the file this writer created belongs to another writer and is
        left alone.
        """
        try:
            ours = self._is_ours()
        except OSError:
            return self._remaining(refusal)  # an inspection that fails confirms nothing
        if not ours:
            return refusal
        try:
            os.unlink(self._sibling)
        except OSError:
            pass  # the inspection below decides whether the sibling is gone
        try:
            still_ours = self._is_ours()
        except OSError:
            return self._remaining(refusal)  # an inspection that fails confirms nothing
        return self._remaining(refusal) if still_ours else refusal

    def _is_ours(self) -> bool:
        """Whether the sibling path holds the file this writer created; absent counts as not."""
        try:
            return _identity(os.lstat(self._sibling)) == self._identity
        except FileNotFoundError:
            return False

    def _remaining(self, refusal: SwapRefused) -> SwapRefused:
        shown = shown_text(str(self._sibling))
        return type(refusal)(
            reason=(
                f"{refusal.reason}; {shown} is not confirmed gone and may hold all or part of"
                " the new login; remove it"
            ),
            sibling_remains=True,
        )


def reserve_swap(path: Path) -> ReservedSwap | SwapRefused:
    """Create the sibling of ``path`` exclusively and hold it; reads and keeps no content."""
    sibling = swap_sibling_or_refusal(path)
    if isinstance(sibling, SwapRefused):
        return sibling
    if os.path.ismount(path):
        return SwapRefused(
            f"{shown_text(str(path))} is a mount point, which a rename cannot replace; bind the"
            " directory that holds the file, read-write",
            False,
        )
    try:
        descriptor = os.open(
            sibling, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, LOGIN_FILE_MODE
        )
    except FileExistsError:
        return SiblingHeld(held_sibling_sentence(sibling), False)
    except OSError as error:
        return SwapRefused(f"{shown_text(str(sibling))} cannot be created: {error.strerror}", False)
    except Exception as error:
        return SwapRefused(f"failed: {type(error).__name__}", False)
    try:
        identity = _identity(os.fstat(descriptor))
    except OSError as error:
        _close_quietly(descriptor)
        return SwapRefused(
            f"{shown_text(str(sibling))} was created but cannot be inspected: {error.strerror};"
            f" it is not confirmed to be this writer's and is left in place; {_RECOVERY}",
            True,
        )
    return ReservedSwap(path, sibling, descriptor, identity)


def swap_in_login_file(
    path: Path, content: bytes, read_back: Callable[[Path], object]
) -> SwapResult:
    """Make ``path`` hold ``content`` once ``read_back`` accepted it, whatever it held before."""
    reservation = reserve_swap(path)
    if isinstance(reservation, SwapRefused):
        return reservation
    return reservation.commit(content, read_back, replaces=None)


def _close_quietly(descriptor: int) -> None:
    """Closes ``descriptor``; a failed close changes no outcome."""
    with contextlib.suppress(OSError):
        os.close(descriptor)


def _identity(status: os.stat_result) -> Identity:
    return (status.st_dev, status.st_ino)


def _holds(path: Path, content: bytes) -> bool:
    """Whether ``path`` is readable as a secret file and holds exactly ``content``."""
    try:
        return read_secret_file(path, "login file") == content
    except UnsoundSecretFile:
        return False


def _directory_synced(directory: Path) -> SwappedDurably | SwappedDurabilityUnconfirmed:
    """Syncs the directory so the rename survives a crash; the rename stays whatever happens."""
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        return SwappedDurabilityUnconfirmed(
            f"{shown_text(str(directory))} was not synced: {error.strerror}"
        )
    return SwappedDurably()


def _refusal_about_the_login_file(path: Path, sibling: Path, reader_text: str) -> str:
    """The reader names the sibling it read; the operator is told about the login file instead."""
    about_sibling = f"{shown_text(str(sibling))} "
    return (
        f"the new document for {shown_text(str(path))} was refused: "
        f"{reader_text.removeprefix(about_sibling)}"
    )
