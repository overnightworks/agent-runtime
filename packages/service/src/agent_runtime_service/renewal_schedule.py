"""When the service renews a provider's dedicated login: at its start, then on a fixed cadence.

One schedule owns one worker thread, so attempts never overlap and the providers run in
sequence; the event loop keeps serving while an attempt runs. A round with no failing report
waits ``RENEWAL_INTERVAL_SECONDS``; a round in which any report is a failure, or an attempt
raised, waits ``RENEWAL_BACKOFF_SECONDS``. Stopping ends the scheduling, waits for the attempt
in flight, then hands every renewer over as interrupted; it never cuts an attempt short.

Every fact reaches ``renewals_of`` as the ``[cli]`` section; nothing reads installed
configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

from agent_runtime_service.renewal import RENEWALS, ProviderRenewer, RenewalReport

if TYPE_CHECKING:
    from agent_runtime_service.config import CliConfig, Provider

RENEWAL_INTERVAL_SECONDS: Final = 15 * 60
RENEWAL_BACKOFF_SECONDS: Final = 60 * 60

log = logging.getLogger(__name__)

Pause = Callable[[float, asyncio.Event], Awaitable[None]]
"""Waits the given seconds, or returns early once the event is set."""


async def wait_for_stop(seconds: float, stopping: asyncio.Event) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stopping.wait(), seconds)


class RenewalSchedule:
    """The rounds of attempts of every renewer, run on one worker thread the schedule owns."""

    def __init__(
        self, renewers: Mapping[Provider, ProviderRenewer], *, pause: Pause = wait_for_stop
    ) -> None:
        self._renewers = dict(renewers)
        self._pause = pause

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Start the rounds on entry and return at once; on exit, stop and hand over.

        Leaving sets the stop, waits for the rounds (and so for the attempt in flight), then
        hands each renewer over on the same worker, containing an exception. A cancellation
        arriving meanwhile interrupts neither wait; it is raised once the hand-over is done.
        """
        if not self._renewers:
            yield
            return
        stopping = asyncio.Event()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="renewal") as worker:
            rounds = asyncio.create_task(self._rounds(worker, stopping))
            try:
                yield
            finally:
                stopping.set()
                try:
                    await _awaited_despite_cancellation(rounds)
                finally:
                    await _awaited_despite_cancellation(
                        asyncio.get_running_loop().run_in_executor(worker, self._hand_over_all)
                    )

    async def _rounds(self, worker: ThreadPoolExecutor, stopping: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stopping.is_set():
            failed = False
            for provider, renewer in self._renewers.items():
                if stopping.is_set():
                    break
                report = await loop.run_in_executor(worker, _attempted, provider, renewer)
                failed |= report is None or report.outcome.is_failure
            wait = RENEWAL_BACKOFF_SECONDS if failed else RENEWAL_INTERVAL_SECONDS
            await self._pause(wait, stopping)

    def _hand_over_all(self) -> None:
        for provider, renewer in self._renewers.items():
            try:
                renewer.hand_over_as_interrupted()
            except Exception as error:  # noqa: BLE001 - one renewer's fault must not skip the rest
                _log_raised("hand-over", provider, error)


async def _awaited_despite_cancellation(awaited: asyncio.Future[object]) -> None:
    """Waits until ``awaited`` is done; a cancellation meanwhile is raised once it is."""
    try:
        await asyncio.wait({awaited})
    except asyncio.CancelledError:
        await _awaited_despite_cancellation(awaited)
        raise


def _attempted(provider: Provider, renewer: ProviderRenewer) -> RenewalReport | None:
    """One attempt, or None when it raised: the exception is logged by its type name only."""
    try:
        return renewer.attempt()
    except Exception as error:  # noqa: BLE001 - an unexpected fault is a failed round and no crash
        _log_raised("renewal", provider, error)
        return None


def _log_raised(doing: str, provider: Provider, error: Exception) -> None:
    log.error("%s of %s raised %s", doing, provider.value, type(error).__name__)


def renewals_of(cli: CliConfig) -> RenewalSchedule:
    """One renewer for each provider that renews and whose program ``cli`` sets.

    Touches no file and starts nothing.
    """
    renewers = {
        provider: ProviderRenewer(
            provider,
            RENEWALS[provider],
            binary=pin.binary,
            token_file=pin.token_file,
            working_root=cli.working_root,
            wall_clock=lambda: int(time.time()),
            monotonic_clock=time.monotonic,
        )
        for provider, pin in cli.programs().items()
        if provider in RENEWALS
    }
    return RenewalSchedule(renewers)
