"""The renewal schedule: its cadence and backoff, containment, the one worker, and the stop.

Every row drives the real ``ProviderRenewer`` through the fake runner of
``service_renewal_support``, with the schedule's pause replaced by a recording one.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from tests.support.bounds import within_the_bound
from tests.support.provider_test_support import a_codex_login
from tests.support.service_login_support import held_swap
from tests.support.service_renewal_support import (  # noqa: F401 - the autouse fixture
    DUE_ORIGINAL,
    HALF_LIFE,
    LOGIN_NAME,
    NOW,
    RENEWED_LOGIN,
    ROTATED_REFRESH,
    Arrangement,
    BlockedHook,
    Confirmation,
    RecordingPause,
    a_fake_runner,
    a_login_issued_at,
    a_run_outcome,
    an_arrangement,
    logged_levels,
    logged_outcomes,
    pool_filled,
    the_held_ends_confirmed,
    the_secrets_of,
    writing,
)

from agent_runtime_service import renewal_schedule
from agent_runtime_service.config import Provider
from agent_runtime_service.renewal import RenewalOutcome
from agent_runtime_service.renewal_schedule import (
    RENEWAL_BACKOFF_SECONDS,
    RENEWAL_INTERVAL_SECONDS,
    Pause,
    RenewalSchedule,
)

A_MOMENT_SECONDS = 0.2
EXCEPTION_CANARY = "exception-text-canary-6e1f"


def a_schedule(*arrangements: Arrangement, pause: Pause | None = None) -> RenewalSchedule:
    providers = (Provider.OPENAI, Provider.ANTHROPIC)
    renewers = {
        provider: arrangement.renewer
        for provider, arrangement in zip(providers, arrangements, strict=False)
    }
    return RenewalSchedule(renewers) if pause is None else RenewalSchedule(renewers, pause=pause)


def two_arrangements(tmp_path: Path) -> tuple[Arrangement, Arrangement]:
    first, second = (tmp_path / "first", tmp_path / "second")
    first.mkdir()
    second.mkdir()
    return an_arrangement(first), an_arrangement(second)


def run_rounds(schedule: RenewalSchedule, pause: RecordingPause) -> None:
    """Run the schedule until its recording pause has stopped it, then leave it."""

    async def scenario() -> None:
        async with schedule.running():
            await within_the_bound(pause.exhausted.wait())

    asyncio.run(scenario())


async def serving_until(schedule: RenewalSchedule, leave: asyncio.Event) -> None:
    async with schedule.running():
        await leave.wait()


async def finishes_within_a_moment(task: asyncio.Task[None]) -> bool:
    done, _ = await asyncio.wait({task}, timeout=A_MOMENT_SECONDS)
    return bool(done)


def stop_during_an_attempt(schedule: RenewalSchedule, hook: BlockedHook) -> None:
    """Leave the schedule while the hook holds an attempt, then let the attempt go."""

    async def scenario() -> None:
        leave = asyncio.Event()
        serving = asyncio.create_task(serving_until(schedule, leave))
        await hook.reached()
        leave.set()
        hook.release()
        await within_the_bound(serving)

    asyncio.run(scenario())


def schedule_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == renewal_schedule.log.name]


CLEAN_ROUNDS = {
    "a renewal": (DUE_ORIGINAL, writing(RENEWED_LOGIN), 1),
    "a login not yet due": (a_login_issued_at(NOW), None, 0),
}


@pytest.mark.parametrize("case", CLEAN_ROUNDS)
def test_a_round_without_a_failure_waits_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    source, on_home, children = CLEAN_ROUNDS[case]
    arrangement = an_arrangement(tmp_path, source)
    runner = a_fake_runner(monkeypatch, on_home=on_home)
    pause = RecordingPause(1)

    run_rounds(a_schedule(arrangement, pause=pause), pause)

    assert pause.seconds == [RENEWAL_INTERVAL_SECONDS]
    assert runner.calls == children


@contextmanager
def a_refresh_that_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Arrangement]:
    a_fake_runner(monkeypatch)
    yield an_arrangement(tmp_path, a_codex_login())


@contextmanager
def a_sibling_held_by_another_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Arrangement]:
    a_fake_runner(monkeypatch)
    arrangement = an_arrangement(tmp_path)
    holder = held_swap(arrangement.token_file)
    try:
        yield arrangement
    finally:
        holder.abandon()


@contextmanager
def a_directory_that_cannot_hold_a_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Arrangement]:
    a_fake_runner(monkeypatch)
    arrangement = an_arrangement(tmp_path)
    arrangement.token_file.parent.chmod(0o500)
    try:
        yield arrangement
    finally:
        arrangement.token_file.parent.chmod(0o700)


@contextmanager
def a_full_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Arrangement]:
    a_fake_runner(monkeypatch, on_home=writing(RENEWED_LOGIN))
    with pool_filled():
        yield an_arrangement(tmp_path)


@contextmanager
def a_login_written_but_not_renewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Arrangement]:
    still_due = a_login_issued_at(NOW - HALF_LIFE, refresh_token=ROTATED_REFRESH, subject="stale")
    a_fake_runner(monkeypatch, on_home=writing(still_due))
    yield an_arrangement(tmp_path)


@contextmanager
def an_end_nobody_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Arrangement]:
    a_fake_runner(
        monkeypatch, outcome=a_run_outcome(became_zombie=True), confirmation=Confirmation.NEVER
    )
    yield an_arrangement(tmp_path)


FAILING_ROUNDS = {
    RenewalOutcome.REFRESH_FAILED: a_refresh_that_fails,
    RenewalOutcome.SIBLING_HELD: a_sibling_held_by_another_writer,
    RenewalOutcome.RESERVE_REFUSED: a_directory_that_cannot_hold_a_sibling,
    RenewalOutcome.POOL_FULL: a_full_pool,
    RenewalOutcome.WRITTEN_NOT_RENEWED: a_login_written_but_not_renewed,
    RenewalOutcome.END_UNCONFIRMED: an_end_nobody_confirms,
}


ROUNDS_THAT_FAIL = [
    pytest.param(
        outcome,
        marks=pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any directory"),
    )
    if outcome is RenewalOutcome.RESERVE_REFUSED
    else outcome
    for outcome in FAILING_ROUNDS
]


@pytest.mark.parametrize("outcome", ROUNDS_THAT_FAIL, ids=lambda outcome: outcome.name)
def test_a_round_with_a_failed_report_waits_the_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: RenewalOutcome,
) -> None:
    caplog.set_level(logging.DEBUG)
    pause = RecordingPause(1)

    with FAILING_ROUNDS[outcome](tmp_path, monkeypatch) as arrangement:
        run_rounds(a_schedule(arrangement, pause=pause), pause)

    assert logged_outcomes(caplog)[0] is outcome
    assert pause.seconds == [RENEWAL_BACKOFF_SECONDS]


def test_a_login_file_that_is_a_mount_point_is_refused_at_the_reserve_and_spends_no_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(monkeypatch, on_home=writing(RENEWED_LOGIN))
    real_ismount = os.path.ismount
    monkeypatch.setattr(
        os.path, "ismount", lambda path: Path(path) == arrangement.token_file or real_ismount(path)
    )
    pause = RecordingPause(1)

    run_rounds(a_schedule(arrangement, pause=pause), pause)

    assert logged_outcomes(caplog) == [RenewalOutcome.RESERVE_REFUSED]
    assert runner.calls == 0
    assert arrangement.directory_holds() == [LOGIN_NAME]
    assert pause.seconds == [RENEWAL_BACKOFF_SECONDS]


def test_a_success_after_a_failure_returns_to_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrangement = an_arrangement(tmp_path)
    attempts = itertools.count()

    def renewing_from_the_second_attempt(home: Path) -> None:
        if next(attempts) > 0:
            writing(RENEWED_LOGIN)(home)

    a_fake_runner(monkeypatch, on_home=renewing_from_the_second_attempt)
    pause = RecordingPause(2)

    run_rounds(a_schedule(arrangement, pause=pause), pause)

    assert pause.seconds == [RENEWAL_BACKOFF_SECONDS, RENEWAL_INTERVAL_SECONDS]


def raising_with_the_canary(_home: Path) -> None:
    raise RuntimeError(EXCEPTION_CANARY)


def test_an_unexpected_exception_is_logged_by_type_and_counts_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=raising_with_the_canary)
    pause = RecordingPause(2)

    run_rounds(a_schedule(arrangement, pause=pause), pause)

    (record,) = schedule_records(caplog)
    assert record.levelno == logging.ERROR
    assert Provider.OPENAI.value in record.getMessage()
    assert RuntimeError.__name__ in record.getMessage()
    assert EXCEPTION_CANARY not in record.getMessage()
    assert pause.seconds == [RENEWAL_BACKOFF_SECONDS] * 2
    assert logged_outcomes(caplog) == [RenewalOutcome.SIBLING_HELD]


def test_no_record_carries_a_token_or_the_text_of_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=raising_with_the_canary)
    pause = RecordingPause(2)

    run_rounds(a_schedule(arrangement, pause=pause), pause)

    for canary in (*the_secrets_of(DUE_ORIGINAL, RENEWED_LOGIN), EXCEPTION_CANARY):
        assert canary not in caplog.text


def test_a_stop_while_pausing_does_not_wait_out_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    arrangement = an_arrangement(tmp_path, a_login_issued_at(NOW))
    runner = a_fake_runner(monkeypatch)
    schedule = a_schedule(arrangement)

    async def scenario() -> None:
        async with schedule.running():
            while not logged_outcomes(caplog):
                await asyncio.sleep(0)

    asyncio.run(within_the_bound(scenario()))

    assert logged_outcomes(caplog) == [RenewalOutcome.NOT_DUE]
    assert runner.calls == 0


def test_a_stop_during_an_attempt_waits_for_it_and_starts_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrangement = an_arrangement(tmp_path)
    hook = BlockedHook(writing(RENEWED_LOGIN))
    runner = a_fake_runner(monkeypatch, on_home=hook)
    schedule = a_schedule(arrangement, pause=RecordingPause(2))

    async def scenario() -> bool:
        leave = asyncio.Event()
        serving = asyncio.create_task(serving_until(schedule, leave))
        await hook.reached()
        leave.set()
        finished_early = await finishes_within_a_moment(serving)
        hook.release()
        await within_the_bound(serving)
        return finished_early

    assert asyncio.run(scenario()) is False
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN
    assert arrangement.directory_holds() == [LOGIN_NAME]
    assert arrangement.trees() == []
    assert runner.calls == 1


def test_a_stop_during_the_first_providers_attempt_skips_the_providers_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = two_arrangements(tmp_path)
    hook = BlockedHook()
    runner = a_fake_runner(monkeypatch, on_home=hook)
    schedule = a_schedule(first, second, pause=RecordingPause(2))

    stop_during_an_attempt(schedule, hook)

    assert runner.calls == 1


def test_a_stop_after_an_unconfirmed_end_hands_it_over_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with an_end_nobody_confirms(tmp_path, monkeypatch) as arrangement:
        pause = RecordingPause(1)
        run_rounds(a_schedule(arrangement, pause=pause), pause)

    assert logged_outcomes(caplog) == [RenewalOutcome.END_UNCONFIRMED, RenewalOutcome.INTERRUPTED]
    assert logged_levels(caplog)[-1] == logging.ERROR
    assert arrangement.directory_holds() == arrangement.directory_holds_login_and_sibling()
    assert len(arrangement.trees()) == 1
    assert arrangement.token_file.read_bytes() == arrangement.source_bytes


def test_a_stop_after_a_late_confirmation_settles_the_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(
        monkeypatch, on_home=writing(RENEWED_LOGIN), confirmation=Confirmation.LATER
    )
    pause = RecordingPause(1)
    schedule = a_schedule(arrangement, pause=pause)

    async def scenario() -> None:
        async with schedule.running():
            await within_the_bound(pause.exhausted.wait())
            runner.confirm_late()

    asyncio.run(scenario())

    assert logged_outcomes(caplog) == [RenewalOutcome.END_UNCONFIRMED, RenewalOutcome.RENEWED]
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN
    assert arrangement.trees() == []


def test_a_stop_during_an_attempt_that_ends_unconfirmed_hands_it_over_after_the_runner_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    hook = BlockedHook()
    a_fake_runner(
        monkeypatch,
        on_home=hook,
        outcome=a_run_outcome(became_zombie=True),
        confirmation=Confirmation.NEVER,
    )
    arrangement = an_arrangement(tmp_path)
    schedule = a_schedule(arrangement, pause=RecordingPause(2))

    stop_during_an_attempt(schedule, hook)

    assert logged_outcomes(caplog) == [RenewalOutcome.END_UNCONFIRMED, RenewalOutcome.INTERRUPTED]
    assert arrangement.directory_holds() == arrangement.directory_holds_login_and_sibling()


def test_a_handover_that_raises_is_logged_by_type_and_the_other_renewers_are_still_handed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    first, second = two_arrangements(tmp_path)
    a_fake_runner(
        monkeypatch, outcome=a_run_outcome(became_zombie=True), confirmation=Confirmation.NEVER
    )

    def handing_over_raises() -> None:
        raise RuntimeError(EXCEPTION_CANARY)

    monkeypatch.setattr(first.renewer, "hand_over_as_interrupted", handing_over_raises)
    pause = RecordingPause(1)

    run_rounds(a_schedule(first, second, pause=pause), pause)

    (record,) = schedule_records(caplog)
    assert record.levelno == logging.ERROR
    assert RuntimeError.__name__ in record.getMessage()
    assert EXCEPTION_CANARY not in record.getMessage()
    assert logged_outcomes(caplog) == [
        RenewalOutcome.END_UNCONFIRMED,
        RenewalOutcome.END_UNCONFIRMED,
        RenewalOutcome.INTERRUPTED,
    ]


def test_a_cancelled_task_still_waits_for_the_attempt_in_flight_and_starts_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrangement = an_arrangement(tmp_path)
    hook = BlockedHook(writing(RENEWED_LOGIN))
    runner = a_fake_runner(monkeypatch, on_home=hook)
    schedule = a_schedule(arrangement, pause=RecordingPause(2))

    async def scenario() -> bool:
        serving = asyncio.create_task(serving_until(schedule, asyncio.Event()))
        await hook.reached()
        serving.cancel()
        finished_early = await finishes_within_a_moment(serving)
        hook.release()
        with pytest.raises(asyncio.CancelledError):
            await within_the_bound(serving)
        return finished_early

    assert asyncio.run(scenario()) is False
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN
    assert arrangement.directory_holds() == [LOGIN_NAME]
    assert arrangement.trees() == []
    assert runner.calls == 1


def an_attempt_nobody_confirms(hook: BlockedHook, monkeypatch: pytest.MonkeyPatch) -> None:
    a_fake_runner(
        monkeypatch,
        on_home=hook,
        outcome=a_run_outcome(became_zombie=True),
        confirmation=Confirmation.NEVER,
    )


async def cancelled_while_it_waits(task: asyncio.Task[None]) -> bool:
    """Cancels the task and tells whether it finished within a moment anyway."""
    task.cancel()
    return await finishes_within_a_moment(task)


def test_a_cancellation_during_the_wait_for_an_attempt_still_hands_it_over_then_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    hook = BlockedHook()
    an_attempt_nobody_confirms(hook, monkeypatch)
    arrangement = an_arrangement(tmp_path)
    schedule = a_schedule(arrangement, pause=RecordingPause(2))

    async def scenario() -> bool:
        leave = asyncio.Event()
        serving = asyncio.create_task(serving_until(schedule, leave))
        await hook.reached()
        leave.set()
        await finishes_within_a_moment(serving)
        finished_early = await cancelled_while_it_waits(serving)
        hook.release()
        with pytest.raises(asyncio.CancelledError):
            await within_the_bound(serving)
        return finished_early

    assert asyncio.run(scenario()) is False
    assert logged_outcomes(caplog) == [RenewalOutcome.END_UNCONFIRMED, RenewalOutcome.INTERRUPTED]
    assert logged_levels(caplog)[-1] == logging.ERROR
    assert arrangement.directory_holds() == arrangement.directory_holds_login_and_sibling()
    assert len(arrangement.trees()) == 1


def test_a_second_cancellation_during_the_handover_still_lets_it_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    hook = BlockedHook()
    an_attempt_nobody_confirms(hook, monkeypatch)
    arrangement = an_arrangement(tmp_path)
    handing_over = BlockedHook()
    hand_over = arrangement.renewer.hand_over_as_interrupted

    def blocked_hand_over() -> None:
        handing_over(Path())
        hand_over()

    monkeypatch.setattr(arrangement.renewer, "hand_over_as_interrupted", blocked_hand_over)
    schedule = a_schedule(arrangement, pause=RecordingPause(2))

    async def scenario() -> bool:
        leave = asyncio.Event()
        serving = asyncio.create_task(serving_until(schedule, leave))
        await hook.reached()
        leave.set()
        await cancelled_while_it_waits(serving)
        hook.release()
        await handing_over.reached()
        finished_early = await cancelled_while_it_waits(serving)
        handing_over.release()
        with pytest.raises(asyncio.CancelledError):
            await within_the_bound(serving)
        return finished_early

    assert asyncio.run(scenario()) is False
    assert logged_outcomes(caplog) == [RenewalOutcome.END_UNCONFIRMED, RenewalOutcome.INTERRUPTED]
    assert arrangement.directory_holds() == arrangement.directory_holds_login_and_sibling()


class OverlapProbe:
    """An ``on_home`` hook that records how many attempts ran at once and on which threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.most_at_once = 0
        self.attempts = 0
        self.threads: set[int] = set()

    def __call__(self, _home: Path) -> None:
        with self._lock:
            self._active += 1
            self.attempts += 1
            self.most_at_once = max(self.most_at_once, self._active)
            self.threads.add(threading.get_ident())
        threading.Event().wait(0.02)
        with self._lock:
            self._active -= 1


def test_attempts_run_one_at_a_time_in_sequence_on_one_thread_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = two_arrangements(tmp_path)
    probe = OverlapProbe()
    a_fake_runner(monkeypatch, on_home=probe)
    pause = RecordingPause(2)
    loop_threads: list[int] = []
    schedule = a_schedule(first, second, pause=pause)

    async def scenario() -> None:
        loop_threads.append(threading.get_ident())
        async with schedule.running():
            await within_the_bound(pause.exhausted.wait())

    asyncio.run(scenario())

    assert probe.attempts == 4
    assert probe.most_at_once == 1
    assert len(probe.threads) == 1
    assert probe.threads.isdisjoint(loop_threads)


def test_a_login_that_keeps_being_written_but_not_renewed_is_attempted_once_per_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    arrangement = an_arrangement(tmp_path)
    versions = itertools.count()

    def writing_another_login_that_is_still_due(home: Path) -> None:
        version = next(versions)
        writing(
            a_login_issued_at(
                NOW - HALF_LIFE, refresh_token=f"rotated-{version}", subject=f"stale-{version}"
            )
        )(home)

    runner = a_fake_runner(monkeypatch, on_home=writing_another_login_that_is_still_due)
    pause = RecordingPause(3)

    run_rounds(a_schedule(arrangement, pause=pause), pause)

    assert logged_outcomes(caplog) == [RenewalOutcome.WRITTEN_NOT_RENEWED] * 3
    assert pause.seconds == [RENEWAL_BACKOFF_SECONDS] * 3
    assert runner.calls == 3
