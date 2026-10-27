"""The one writer of a login file: a new document swapped in beside the old one.

The document is written to a sibling of the destination (``rename(2)`` refuses across mount
points), read back by the profile's own reader on that very file, and only then renamed over the
old file. A failure before the rename leaves the old file byte for byte.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_runtime.secret_file import UnsoundSecretFile, shown_text

LOGIN_FILE_MODE: Final = 0o600


def swap_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.new")


@dataclass(frozen=True)
class SwapRefused:
    """Why nothing was swapped in: a closed reason, and whether the writer's sibling remains."""

    reason: str
    sibling_remains: bool


def swap_in_login_file(
    path: Path, content: bytes, read_back: Callable[[Path], object]
) -> SwapRefused | None:
    """Make ``path`` hold ``content`` once ``read_back`` accepted it; ``None`` means swapped in.

    Never raises an ``Exception``: every refusal is returned as a closed reason that holds no
    part of ``content``. The caller must have no asynchronous exception arising in this thread:
    the login command disregards interrupts, and a worker thread never receives a signal.
    The rename is the commit point; nothing after it can fail the swap. The swap is atomic, not
    durable: neither the file nor its directory is synced.
    """
    sibling = swap_sibling(path)
    try:
        descriptor = os.open(
            sibling, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, LOGIN_FILE_MODE
        )
    except OSError as error:
        return SwapRefused(f"{shown_text(str(sibling))} cannot be created: {error.strerror}", False)
    except Exception as error:
        return SwapRefused(f"failed: {type(error).__name__}", False)
    reason = _reason_not_swapped(path, sibling, descriptor, content, read_back)
    if reason is None:
        return None
    return _with_sibling_removed(sibling, reason)


def _reason_not_swapped(
    path: Path,
    sibling: Path,
    descriptor: int,
    content: bytes,
    read_back: Callable[[Path], object],
) -> str | None:
    """Writes, reads back and renames the sibling; the reason text if any step failed."""
    try:
        with open(descriptor, "wb") as new_file:
            new_file.write(content)
        read_back(sibling)
        os.replace(sibling, path)
    except UnsoundSecretFile as refusal:
        return str(refusal)
    except OSError as error:
        return f"{shown_text(str(path))} cannot be swapped in: {error.strerror}"
    except Exception as error:
        return f"failed: {type(error).__name__}"
    return None


def _with_sibling_removed(sibling: Path, reason: str) -> SwapRefused:
    """Unlinks the writer's own sibling and reports it unless ``lstat`` shows it gone."""
    try:
        os.unlink(sibling)
    except OSError:
        pass  # the lstat below decides whether the sibling is gone
    try:
        os.lstat(sibling)
    except FileNotFoundError:
        return SwapRefused(reason, False)
    except OSError:
        pass  # an inspection that fails confirms nothing
    shown = shown_text(str(sibling))
    return SwapRefused(
        f"{reason}; {shown} is not confirmed gone and may hold all or part of the new login;"
        " remove it",
        True,
    )
