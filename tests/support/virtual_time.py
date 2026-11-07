"""The one synthetic event-loop clock the suites share; it imports no extra."""

from __future__ import annotations

import asyncio
import selectors
from collections.abc import Coroutine
from typing import Any


class _JumpingSelector(selectors.DefaultSelector):
    """Polls instead of sleeping, and moves its loop's clock on by the sleep it skipped."""

    def __init__(self, loop: VirtualTimeLoop) -> None:
        super().__init__()
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        if timeout is None:
            raise RuntimeError("the loop has nothing left to run and no timer to wait for")
        ready = super().select(0)
        if not ready:
            self._loop.skew_seconds += timeout
        return ready


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock is synthetic and jumps to the next timer whenever it would sleep.

    Only the selector moves the clock, so real scheduling delays never show in a measurement.
    Deadlines and timeouts pass in no real time, and a call that would block forever fails
    loud instead of hanging the suite.
    """

    def __init__(self) -> None:
        self.skew_seconds = 0.0
        super().__init__(_JumpingSelector(self))

    def time(self) -> float:
        return self.skew_seconds


def in_virtual_time[T](main: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(main, loop_factory=VirtualTimeLoop)
