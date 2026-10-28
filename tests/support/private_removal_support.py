"""What a test refuses or leaves behind when a private run tree is removed."""

from __future__ import annotations

import errno
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_runtime import process

REMOVAL_FAILED = "private credential home removal failed (PermissionError)"


def refuse_to_remove(_path: Any, *_arguments: Any, **_keywords: Any) -> None:
    raise PermissionError(errno.EACCES, "synthetic refusal")


@contextmanager
def rmtree_refused(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with monkeypatch.context() as patched:
        patched.setattr(process.shutil, "rmtree", refuse_to_remove)
        yield


def remove_the_leftover(tree: Path) -> None:
    shutil.rmtree(tree)
