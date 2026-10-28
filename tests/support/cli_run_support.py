"""The scripted ``CliRunAdapter`` and the arrangement every ``astream_cli_run`` test module shares.

The adapter starts a ``/bin/sh`` leader through the real door; its decoder reads ``say:``,
``call:`` and ``fail:`` lines. The fixtures are named in their decorators: a test module imports
the functions it uses.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from tests.support.anchored_leaders import spawn_warnings
from tests.support.anchored_support import GONE, ChildCleanup
from tests.support.provider_test_support import (
    REAL_REAP_BOUNDS,
    a_private_root,
    drive_real_children,
    held_slots,
)

from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.cli_run import CliRunStart, StepEvent
from agent_runtime.config import PrivateWorkingRoot
from agent_runtime.events import AssistantTextEvent, FinalEvent, ToolCallEvent
from agent_runtime.spawn import ChildProcess, closed_environment

SHELL = Path("/bin/sh")


class ScriptRefused(Exception):
    """The decoder's closed failure at a ``fail`` line."""


class EndingRefused(Exception):
    """The decoder's failure for a run that did not end cleanly."""


def text(value: str) -> AssistantTextEvent:
    return AssistantTextEvent(text=value)


def ended(returncode: int) -> FinalEvent:
    return FinalEvent(text=f"Final rc={returncode}")


class LineScriptDecoder:
    def __init__(self, refusal: Exception, refuses_failed_ending: bool) -> None:
        self._refusal = refusal
        self._refuses_failed_ending = refuses_failed_ending
        self.returncodes: list[int] = []

    def events_of(self, line: bytes) -> list[StepEvent]:
        kind, _, value = line.decode().strip().partition(":")
        if kind == "say":
            return [text(value)]
        if kind == "call":
            return [ToolCallEvent(tool_use_id=value, name="tool", input={})]
        if kind == "fail":
            raise self._refusal
        return []

    def ending(self, returncode: int) -> FinalEvent:
        self.returncodes.append(returncode)
        if self._refuses_failed_ending and returncode != 0:
            raise EndingRefused
        return ended(returncode)


@dataclass
class LineScriptAdapter:
    pool: CliProcessPool
    script: str = ""
    stdin: bytes = b""
    files: Mapping[str, bytes] = field(default_factory=dict)
    line_limit_bytes: int = 1024 * 1024
    binary: Path = SHELL
    refusal: Exception = field(default_factory=ScriptRefused)
    start_failure: Exception | None = None
    refuses_failed_ending: bool = False
    decoders: list[LineScriptDecoder] = field(default_factory=list)

    def run_files(self) -> Mapping[str, bytes]:
        return self.files

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        if self.start_failure is not None:
            raise self.start_failure
        decoder = LineScriptDecoder(self.refusal, self.refuses_failed_ending)
        self.decoders.append(decoder)
        child = ChildProcess(
            binary=self.binary,
            arguments=("-c", self.script),
            environment=closed_environment(home),
            working_directory=working_directory,
        )
        return CliRunStart(child, self.stdin, decoder)


@pytest.fixture(name="private_root")
def a_private_working_root(tmp_path: Path) -> Path:
    return a_private_root(tmp_path)


@pytest.fixture(name="root")
def a_root(private_root: Path) -> PrivateWorkingRoot:
    return PrivateWorkingRoot(private_root)


@pytest.fixture(name="pool")
def a_pool() -> CliProcessPool:
    return CliProcessPool(limit=1, name="cli run test", reap=REAL_REAP_BOUNDS)


@pytest.fixture(name="drive")
def a_drive(
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
