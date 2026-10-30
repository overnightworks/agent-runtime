"""One admission owner for every Codex CLI process."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum

from agent_runtime.config import current_codex_turn_runtime
from agent_runtime.errors import CodexProcessPoolSaturatedError


class CodexProcessKind(StrEnum):
    """The Codex process classes."""

    TEXT = "text"


@dataclass(eq=False)
class CodexProcessReservation:
    """A reserved process slot, optionally bound to the CLI process PID."""

    kind: CodexProcessKind
    pid: int | None = None


class CodexProcessPool:
    """Keep reserved, live, and zombie Codex processes within their caps."""

    def __init__(self, *, maximum_processes: int) -> None:
        if maximum_processes < 1:
            raise ValueError("Codex process cap must be at least one")
        self._maximum_processes = maximum_processes
        self._lock = threading.Lock()
        self._reservations: set[CodexProcessReservation] = set()

    def reserve(self, kind: CodexProcessKind) -> CodexProcessReservation:
        """Reserve before spawning, including while a killed process is unreaped."""
        with self._lock:
            if len(self._reservations) >= self._maximum_processes:
                raise CodexProcessPoolSaturatedError(kind=kind, scope="total")
            reservation = CodexProcessReservation(kind=kind)
            self._reservations.add(reservation)
            return reservation

    def bind(self, reservation: CodexProcessReservation, process_id: int) -> None:
        """Bind a reservation only after the bounded runner has spawned."""
        if process_id < 1:
            raise ValueError("Cannot bind a Codex process reservation without a PID")
        with self._lock:
            if reservation not in self._reservations:
                raise RuntimeError("Cannot bind a released Codex process reservation")
            reservation.pid = process_id

    def reap(self, reservation: CodexProcessReservation, process_id: int) -> None:
        """Free a slot only after the bounded runner reports the process reaped."""
        with self._lock:
            if reservation not in self._reservations:
                return
            if reservation.pid != process_id:
                raise RuntimeError("Codex process reap did not match its reservation")
            self._reservations.remove(reservation)

    def abandon_unspawned(self, reservation: CodexProcessReservation) -> None:
        """Free an admission only when spawning failed without creating a process."""
        with self._lock:
            if reservation not in self._reservations:
                return
            if reservation.pid is not None:
                return
            self._reservations.remove(reservation)

    def reservation_count(self) -> int:
        """Return all slots, including unbound and zombie reservations."""
        with self._lock:
            return len(self._reservations)


_process_pool: CodexProcessPool | None = None
_process_pool_lock = threading.Lock()


def get_codex_process_pool() -> CodexProcessPool:
    """Return the one process-wide Codex admission owner."""
    global _process_pool
    turns = current_codex_turn_runtime()
    with _process_pool_lock:
        if _process_pool is None:
            _process_pool = CodexProcessPool(
                maximum_processes=turns.max_concurrent_processes,
            )
        return _process_pool
