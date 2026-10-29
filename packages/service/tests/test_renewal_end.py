"""A renewal child whose end is not confirmed: what the renewer keeps, what a late confirmation
settles, and why a confirmation after the handoff writes and removes nothing.

The bounded runner is the fake of ``service_renewal_support``: a process group that outlives
SIGKILL cannot be made by a real child.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from tests.support.service_renewal_support import (  # noqa: F401 - the autouse fixture
    LOGIN_NAME,
    RENEWED_LOGIN,
    Confirmation,
    a_fake_runner,
    a_run_outcome,
    an_arrangement,
    logged_once,
    pool_is_free,
    the_held_ends_confirmed,
    writing,
)

from agent_runtime.process import CliRunReason
from agent_runtime_service.renewal import Cleanup, RenewalOutcome

UNCONFIRMED_ENDS = {
    "a reap that became a zombie": a_run_outcome(became_zombie=True),
    "a cleanup that overran": a_run_outcome(CliRunReason.CLEANUP_OVERRAN),
    "a deadline before the spawn": a_run_outcome(CliRunReason.DEADLINE_BEFORE_SPAWN),
}


@pytest.mark.parametrize("end", UNCONFIRMED_ENDS)
def test_an_unconfirmed_end_keeps_everything_and_blocks_every_later_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, end: str
) -> None:
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(
        monkeypatch, outcome=UNCONFIRMED_ENDS[end], confirmation=Confirmation.NEVER
    )

    first = logged_once(caplog, arrangement.renewer.attempt)
    second = logged_once(caplog, arrangement.renewer.attempt)

    assert (first.outcome, first.cleanup, first.turn) == (
        RenewalOutcome.END_UNCONFIRMED,
        Cleanup.RETAINED,
        UNCONFIRMED_ENDS[end].reason,
    )
    assert second.outcome is RenewalOutcome.END_UNCONFIRMED
    assert runner.calls == 1
    assert arrangement.directory_holds() == [f".{LOGIN_NAME}.new", LOGIN_NAME]
    assert len(arrangement.trees()) == 1
    assert pool_is_free() is False


def test_a_late_confirmation_frees_the_slot_and_the_next_attempt_settles_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(
        monkeypatch,
        outcome=a_run_outcome(became_zombie=True),
        on_home=writing(RENEWED_LOGIN),
        confirmation=Confirmation.LATER,
    )
    logged_once(caplog, arrangement.renewer.attempt)

    runner.confirm_late()

    assert pool_is_free()
    assert arrangement.directory_holds() == [f".{LOGIN_NAME}.new", LOGIN_NAME]
    assert len(arrangement.trees()) == 1
    settled = logged_once(caplog, arrangement.renewer.attempt)
    assert (settled.outcome, settled.cleanup) == (RenewalOutcome.RENEWED, Cleanup.REMOVED)
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN
    assert (arrangement.trees(), runner.calls) == ([], 1)


def test_a_spawn_failure_confirmed_late_is_settled_as_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(
        monkeypatch,
        outcome=a_run_outcome(CliRunReason.DEADLINE_BEFORE_SPAWN),
        confirmation=Confirmation.LATER,
        spawn_failed=True,
    )
    logged_once(caplog, arrangement.renewer.attempt)

    runner.confirm_late()
    settled = logged_once(caplog, arrangement.renewer.attempt)

    assert (settled.outcome, settled.cleanup) == (RenewalOutcome.NOT_STARTED, Cleanup.REMOVED)
    assert (arrangement.source_is_intact(), arrangement.trees(), runner.calls) == (True, [], 1)


def test_a_confirmation_after_the_handoff_writes_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(
        monkeypatch,
        outcome=a_run_outcome(became_zombie=True),
        on_home=writing(RENEWED_LOGIN),
        confirmation=Confirmation.LATER,
    )
    logged_once(caplog, arrangement.renewer.attempt)

    handed_over = logged_once(caplog, arrangement.renewer.hand_over_as_interrupted)
    runner.confirm_late()
    after = logged_once(caplog, arrangement.renewer.attempt)

    assert (handed_over.outcome, handed_over.cleanup) == (
        RenewalOutcome.INTERRUPTED,
        Cleanup.RETAINED,
    )
    assert [
        record.levelno for record in caplog.records if "interrupted" in record.getMessage()
    ] == [
        logging.ERROR,
        logging.ERROR,
    ]
    assert after.outcome is RenewalOutcome.INTERRUPTED
    assert arrangement.directory_holds() == [f".{LOGIN_NAME}.new", LOGIN_NAME]
    assert arrangement.token_file.read_bytes() == arrangement.source_bytes
    assert (len(arrangement.trees()), runner.calls) == (1, 1)


def test_a_handoff_settles_an_end_that_was_confirmed_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(
        monkeypatch,
        outcome=a_run_outcome(became_zombie=True),
        on_home=writing(RENEWED_LOGIN),
        confirmation=Confirmation.LATER,
    )
    logged_once(caplog, arrangement.renewer.attempt)
    runner.confirm_late()

    handed_over = logged_once(caplog, arrangement.renewer.hand_over_as_interrupted)

    assert handed_over.outcome is RenewalOutcome.RENEWED
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN
    assert arrangement.trees() == []


def test_a_handoff_with_nothing_outstanding_returns_nothing(tmp_path: Path) -> None:
    assert an_arrangement(tmp_path).renewer.hand_over_as_interrupted() is None
