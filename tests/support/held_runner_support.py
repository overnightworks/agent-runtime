"""A CLI runner whose runs a test holds open."""

import asyncio
import threading

from tests.support.bounds import TEST_WAIT_SECONDS

from agent_runtime.events import FinalEvent


class HeldRunner:
    """A CLI runner whose runs stay open until a test lets them go, each for a bounded time."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.gates: list[threading.Event] = []
        self._everything_proceeds = threading.Event()

    async def run(self, admitted):
        gate = threading.Event()
        self.gates.append(gate)
        self.entered.set()
        if self._everything_proceeds.is_set():
            gate.set()
        await asyncio.to_thread(gate.wait, TEST_WAIT_SECONDS)
        yield FinalEvent(text="done")

    def proceed_all(self) -> None:
        self._everything_proceeds.set()
        for gate in self.gates:
            gate.set()
