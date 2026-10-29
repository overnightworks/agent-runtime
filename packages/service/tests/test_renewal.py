"""One renewal attempt: its order, D8's judgement of the result, the source and sibling rows, the
commit and the cleanup, and what no log, report, argument or environment may carry.

"Real child" rows run the renewing stand-in CLI through the real bounded runner; the others use
the fake runner of ``service_renewal_support``.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from tests.support.codex_support import a_renewing_fake_codex
from tests.support.private_removal_support import rmtree_refused
from tests.support.provider_test_support import a_codex_login
from tests.support.service_login_support import (  # noqa: F401 - the fixtures are named in their decorators
    LINE_PREFIX,
    Said,
    Stream,
    an_installation,
    held_swap,
    run,
    the_signal_handlers_restored,
    what_was_said,
)
from tests.support.service_renewal_support import (  # noqa: F401 - the autouse fixture
    DUE_ORIGINAL,
    HALF_LIFE,
    LIFETIME,
    LOGIN_NAME,
    NOW,
    RENEWED_LOGIN,
    ROTATED_REFRESH,
    Arrangement,
    Confirmation,
    a_codex_entry,
    a_fake_runner,
    a_login_issued_at,
    an_arrangement,
    held_pool_slots,
    linking_the_login,
    logged_once,
    oversizing_the_login,
    pool_filled,
    removing_the_login,
    the_held_ends_confirmed,
    the_secrets_of,
    writing,
)
from tests.support.service_table_support import write_login

from agent_runtime.codex.renewal import CodexRenewal
from agent_runtime.codex.run import AUTH_FILE_NAME
from agent_runtime.codex_login import CodexRenewalRefusal, UnreadableTiming, blanked_copy
from agent_runtime.config import reset_config
from agent_runtime.process import CliRunReason
from agent_runtime.secret_file import SecretFileFault
from agent_runtime_service import login_file
from agent_runtime_service.config import Provider, read_codex_login
from agent_runtime_service.login_file import held_sibling_sentence
from agent_runtime_service.renewal import Cleanup, NotRenewedCause, RenewalOutcome


def test_a_login_at_exactly_half_its_lifetime_is_renewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path, a_login_issued_at(NOW - HALF_LIFE))
    runner = a_fake_runner(monkeypatch)

    logged_once(caplog, arrangement.renewer.attempt)

    assert runner.calls == 1


def test_a_login_not_yet_due_starts_nothing_and_leaves_no_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path, a_login_issued_at(NOW - HALF_LIFE + 1))
    runner = a_fake_runner(monkeypatch)
    held_before = held_pool_slots()

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.NOT_DUE
    assert (runner.calls, arrangement.trees(), held_pool_slots()) == (0, [], held_before)
    assert arrangement.source_is_intact()


def test_a_real_child_renews_the_login_and_the_commit_replaces_the_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    binary = a_renewing_fake_codex(tmp_path, RENEWED_LOGIN)
    arrangement = an_arrangement(tmp_path, binary=binary)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.durability_confirmed, report.turn, report.cleanup) == (
        RenewalOutcome.RENEWED,
        True,
        CliRunReason.COMPLETE,
        Cleanup.REMOVED,
    )
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN
    assert stat.S_IMODE(arrangement.token_file.stat().st_mode) == 0o600
    assert (arrangement.directory_holds(), arrangement.trees()) == ([LOGIN_NAME], [])


def test_the_renewer_works_after_the_installed_configuration_is_reset(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reset_config()
    arrangement = an_arrangement(tmp_path, binary=a_renewing_fake_codex(tmp_path, RENEWED_LOGIN))

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.RENEWED
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN


def test_an_unreadable_source_timing_renews_at_most_once_in_24_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path, a_codex_login())
    runner = a_fake_runner(monkeypatch)
    day = 24 * 60 * 60

    first = logged_once(caplog, arrangement.renewer.attempt)
    arrangement.clocks.monotonic = day - 60
    held_back = logged_once(caplog, arrangement.renewer.attempt)
    calls_before_the_day = runner.calls
    arrangement.clocks.monotonic = day
    again = logged_once(caplog, arrangement.renewer.attempt)

    assert (first.outcome, first.source_timing) == (
        RenewalOutcome.REFRESH_FAILED,
        UnreadableTiming.NOT_A_TOKEN,
    )
    assert (held_back.outcome, held_back.source_timing, calls_before_the_day) == (
        RenewalOutcome.NOT_DUE,
        UnreadableTiming.NOT_A_TOKEN,
        1,
    )
    assert (again.outcome, runner.calls) == (RenewalOutcome.REFRESH_FAILED, 2)
    assert UnreadableTiming.NOT_A_TOKEN.value in caplog.text


def test_a_timing_that_becomes_readable_clears_the_24_hour_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path, a_codex_login())
    runner = a_fake_runner(monkeypatch)
    logged_once(caplog, arrangement.renewer.attempt)
    write_login(arrangement.token_file, a_login_issued_at(NOW))
    logged_once(caplog, arrangement.renewer.attempt)
    write_login(arrangement.token_file, a_codex_login())
    arrangement.clocks.monotonic = 60.0

    again = logged_once(caplog, arrangement.renewer.attempt)

    assert (again.outcome, runner.calls) == (RenewalOutcome.REFRESH_FAILED, 2)


@pytest.mark.parametrize("confirmation", [Confirmation.NOW, Confirmation.LATER])
def test_a_confirmed_spawn_failure_does_not_use_up_the_24_hour_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    confirmation: Confirmation,
) -> None:
    arrangement = an_arrangement(tmp_path, a_codex_login())
    runner = a_fake_runner(monkeypatch, spawn_failed=True, confirmation=confirmation)
    logged_once(caplog, arrangement.renewer.attempt)
    if confirmation is Confirmation.LATER:
        runner.confirm_late()
        settled = logged_once(caplog, arrangement.renewer.attempt)
        assert settled.outcome is RenewalOutcome.NOT_STARTED
    arrangement.clocks.monotonic = 3600.0

    retried = logged_once(caplog, arrangement.renewer.attempt)

    assert retried.outcome is not RenewalOutcome.NOT_DUE
    assert runner.calls == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any directory")
def test_a_root_that_cannot_hold_a_home_does_not_use_up_the_24_hour_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path, a_codex_login())
    runner = a_fake_runner(monkeypatch)
    arrangement.root.chmod(0o500)
    try:
        failed = logged_once(caplog, arrangement.renewer.attempt)
    finally:
        arrangement.root.chmod(0o700)
    arrangement.clocks.monotonic = 3600.0

    retried = logged_once(caplog, arrangement.renewer.attempt)

    assert (failed.outcome, retried.outcome, runner.calls) == (
        RenewalOutcome.HOME_NOT_CREATED,
        RenewalOutcome.REFRESH_FAILED,
        1,
    )


def test_a_binary_that_cannot_start_does_not_use_up_the_24_hour_allowance(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o644)
    arrangement = an_arrangement(tmp_path, a_codex_login(), binary=binary)
    failed = logged_once(caplog, arrangement.renewer.attempt)
    binary.chmod(0o755)
    arrangement.clocks.monotonic = 3600.0

    retried = logged_once(caplog, arrangement.renewer.attempt)

    assert failed.outcome is RenewalOutcome.NOT_STARTED
    assert retried.outcome is not RenewalOutcome.NOT_DUE


def _failing(_self: CodexRenewal, *_arguments: Any) -> Any:
    raise RuntimeError("injected")


@pytest.mark.parametrize("failing_step", ["run_files", "start"])
def test_a_failure_before_the_runner_frees_the_slot_and_the_home_and_keeps_the_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_step: str,
) -> None:
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(monkeypatch)
    monkeypatch.setattr(CodexRenewal, failing_step, _failing)
    held_before = held_pool_slots()

    with pytest.raises(RuntimeError, match="injected"):
        logged_once(caplog, arrangement.renewer.attempt)

    assert (held_pool_slots(), arrangement.trees(), runner.calls) == (held_before, [], 0)
    assert arrangement.sibling.exists()


# D8: a preservable result is always committed; "renewed" needs the predicate on top.
ORIGINAL_WITH_OPAQUE_TOKEN = a_codex_login(tokens={"refresh_token": "opaque-original-refresh"})
STILL_DUE_AT_NOW = a_login_issued_at(
    NOW - 2 * LIFETIME, lifetime=4 * LIFETIME, refresh_token=ROTATED_REFRESH, subject="due"
)
JUDGEMENTS = {
    "renewed": (DUE_ORIGINAL, RENEWED_LOGIN, RenewalOutcome.RENEWED, None),
    "renewed with an unreadable original timing": (
        ORIGINAL_WITH_OPAQUE_TOKEN,
        RENEWED_LOGIN,
        RenewalOutcome.RENEWED,
        None,
    ),
    "timing unreadable": (
        DUE_ORIGINAL,
        a_codex_login(tokens={"refresh_token": ROTATED_REFRESH}),
        RenewalOutcome.WRITTEN_NOT_RENEWED,
        NotRenewedCause.TIMING_UNREADABLE,
    ),
    "still due": (
        DUE_ORIGINAL,
        a_login_issued_at(NOW - HALF_LIFE, refresh_token=ROTATED_REFRESH, subject="stale"),
        RenewalOutcome.WRITTEN_NOT_RENEWED,
        NotRenewedCause.STILL_DUE,
    ),
    "still due outranks a later expiry": (
        DUE_ORIGINAL,
        STILL_DUE_AT_NOW,
        RenewalOutcome.WRITTEN_NOT_RENEWED,
        NotRenewedCause.STILL_DUE,
    ),
    "refresh token unchanged": (
        DUE_ORIGINAL,
        a_login_issued_at(NOW, subject="same-refresh"),
        RenewalOutcome.WRITTEN_NOT_RENEWED,
        NotRenewedCause.REFRESH_TOKEN_UNCHANGED,
    ),
    "expiry not later": (
        DUE_ORIGINAL,
        a_login_issued_at(NOW, lifetime=100, refresh_token=ROTATED_REFRESH, subject="short"),
        RenewalOutcome.WRITTEN_NOT_RENEWED,
        NotRenewedCause.EXPIRY_NOT_LATER,
    ),
}


@pytest.mark.parametrize("judgement", JUDGEMENTS)
def test_a_preservable_result_is_always_committed_and_called_renewed_only_when_the_predicate_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    judgement: str,
) -> None:
    original, result, outcome, cause = JUDGEMENTS[judgement]
    arrangement = an_arrangement(tmp_path, original)
    a_fake_runner(monkeypatch, on_home=writing(result))

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.cause, report.durability_confirmed) == (outcome, cause, True)
    assert arrangement.token_file.read_bytes() == result
    assert (arrangement.directory_holds(), arrangement.trees()) == ([LOGIN_NAME], [])


def test_a_real_child_that_leaves_an_unreadable_timing_is_committed_as_written_not_renewed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    result = a_codex_login(tokens={"refresh_token": ROTATED_REFRESH})
    arrangement = an_arrangement(tmp_path, binary=a_renewing_fake_codex(tmp_path, result))

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.cause) == (
        RenewalOutcome.WRITTEN_NOT_RENEWED,
        NotRenewedCause.TIMING_UNREADABLE,
    )
    assert arrangement.token_file.read_bytes() == result


def test_a_result_equal_to_the_original_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=writing(DUE_ORIGINAL))

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.NOT_RENEWED
    assert arrangement.source_is_intact()
    assert arrangement.trees() == []


def _the_made_due_copy_with_only_the_last_refresh_changed(home: Path) -> None:
    document = json.loads((home / AUTH_FILE_NAME).read_bytes())
    document["last_refresh"] = "2026-10-29T00:00:00Z"
    (home / AUTH_FILE_NAME).write_text(json.dumps(document))


def _the_login_without_a_refresh_token(home: Path) -> None:
    writing(a_login_issued_at(NOW, refresh_token="", subject="blank"))(home)


NOT_PRESERVABLE = {
    "no refresh token": _the_login_without_a_refresh_token,
    "an empty access token": writing(a_codex_login(tokens={"access_token": ""})),
    "invalid JSON": writing(b"{not json"),
    "incomplete": writing(b'{"auth_mode": "chatgpt"}'),
    "only the made-due copy's last refresh changed": (
        _the_made_due_copy_with_only_the_last_refresh_changed
    ),
}


@pytest.mark.parametrize("result", NOT_PRESERVABLE)
def test_a_result_that_differs_but_is_not_preservable_never_reaches_the_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    result: str,
) -> None:
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=NOT_PRESERVABLE[result])

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.MALFORMED_RESULT
    assert arrangement.source_is_intact()
    assert arrangement.trees() == []


def test_a_child_that_fails_and_leaves_the_copy_is_a_failed_refresh(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    binary = a_renewing_fake_codex(tmp_path, None, then="exit 1")
    arrangement = an_arrangement(tmp_path, binary=binary)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.REFRESH_FAILED
    assert arrangement.source_is_intact()
    assert arrangement.trees() == []


def test_a_source_without_a_refresh_token_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path, a_codex_login(tokens={"refresh_token": ""}))
    runner = a_fake_runner(monkeypatch)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, runner.calls) == (RenewalOutcome.NO_REFRESH_TOKEN, 0)
    assert arrangement.source_is_intact()


def _missing(arrangement: Arrangement) -> None:
    arrangement.token_file.unlink()


def _group_readable(arrangement: Arrangement) -> None:
    arrangement.token_file.chmod(0o644)


def _malformed(arrangement: Arrangement) -> None:
    write_login(arrangement.token_file, b"{not json")


@pytest.mark.parametrize("spoil", [_missing, _group_readable, _malformed])
def test_a_source_the_reader_refuses_starts_nothing_and_names_a_closed_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    spoil: Any,
) -> None:
    arrangement = an_arrangement(tmp_path)
    spoil(arrangement)
    runner = a_fake_runner(monkeypatch)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, runner.calls) == (RenewalOutcome.SOURCE_REFUSED, 0)
    assert report.refusal is not None
    assert str(arrangement.token_file) in report.refusal
    assert "{not json" not in report.refusal
    if spoil is _malformed:
        assert CodexRenewalRefusal.NOT_USABLE.value in report.refusal
    assert arrangement.directory_holds() == ([] if spoil is _missing else [LOGIN_NAME])


def test_a_child_that_writes_and_is_cut_by_the_deadline_is_still_read_back_and_committed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    binary = a_renewing_fake_codex(tmp_path, RENEWED_LOGIN, then="sleep 30")
    arrangement = an_arrangement(tmp_path, binary=binary, entry=a_codex_entry(1.0))

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.turn) == (
        RenewalOutcome.RENEWED,
        CliRunReason.DEADLINE_WHILE_READING,
    )
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN


def test_a_child_that_writes_and_then_fails_is_committed_and_the_turn_reports_the_runners_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    binary = a_renewing_fake_codex(tmp_path, RENEWED_LOGIN, then="exit 1")
    arrangement = an_arrangement(tmp_path, binary=binary)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.turn) == (RenewalOutcome.RENEWED, CliRunReason.COMPLETE)
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN


def test_a_full_pool_fails_the_attempt_and_the_next_one_after_a_release_renews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=writing(RENEWED_LOGIN))

    with pool_filled():
        full = logged_once(caplog, arrangement.renewer.attempt)
        assert (full.outcome, arrangement.trees()) == (RenewalOutcome.POOL_FULL, [])
        assert arrangement.source_is_intact()
    renewed = logged_once(caplog, arrangement.renewer.attempt)

    assert renewed.outcome is RenewalOutcome.RENEWED


def test_a_sibling_held_by_another_writer_blocks_every_attempt_and_is_left_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    holder = held_swap(arrangement.token_file)
    held = os.lstat(arrangement.sibling)
    runner = a_fake_runner(monkeypatch)

    reports = [logged_once(caplog, arrangement.renewer.attempt) for _ in range(2)]

    assert [report.outcome for report in reports] == [RenewalOutcome.SIBLING_HELD] * 2
    assert reports[0].refusal == held_sibling_sentence(arrangement.sibling)
    assert (os.lstat(arrangement.sibling), runner.calls) == (held, 0)
    holder.abandon()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any directory")
def test_a_login_directory_that_cannot_hold_a_sibling_leaves_the_source_intact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    arrangement.token_file.parent.chmod(0o500)
    try:
        report = logged_once(caplog, arrangement.renewer.attempt)
    finally:
        arrangement.token_file.parent.chmod(0o700)

    assert report.outcome is RenewalOutcome.RESERVE_REFUSED
    assert arrangement.source_is_intact()


def test_a_source_rewritten_while_the_child_runs_keeps_its_newer_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    newer = a_login_issued_at(NOW - 5, subject="newer", refresh_token="newer-refresh")
    inode: list[int] = []

    def rewriting(home: Path) -> None:
        writing(RENEWED_LOGIN)(home)
        write_login(arrangement.token_file, newer)
        inode.append(arrangement.token_file.stat().st_ino)

    a_fake_runner(monkeypatch, on_home=rewriting)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.SOURCE_CHANGED
    assert (arrangement.token_file.read_bytes(), arrangement.token_file.stat().st_ino) == (
        newer,
        inode[0],
    )
    assert (arrangement.directory_holds(), arrangement.trees()) == ([LOGIN_NAME], [])


def test_a_refused_rename_keeps_the_rotated_document_in_the_retained_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=writing(RENEWED_LOGIN))

    def failing(*_paths: Any) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(login_file.os, "replace", failing)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.cleanup) == (RenewalOutcome.COMMIT_REFUSED, Cleanup.RETAINED)
    assert report.refusal
    assert "cannot be swapped in" in report.refusal
    assert arrangement.source_is_intact()
    assert arrangement.retained_login() == RENEWED_LOGIN


def test_a_directory_sync_that_fails_after_the_rename_reports_written_without_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=writing(RENEWED_LOGIN))
    real_fsync = os.fsync

    def syncing(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", syncing)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.durability_confirmed) == (RenewalOutcome.RENEWED, False)
    assert arrangement.token_file.read_bytes() == RENEWED_LOGIN


@pytest.mark.parametrize(
    ("spoil", "fault"),
    [
        (removing_the_login, SecretFileFault.MISSING),
        (linking_the_login, SecretFileFault.LINK),
        (oversizing_the_login, SecretFileFault.TOO_LARGE),
    ],
)
def test_a_home_that_cannot_be_read_back_is_retained_and_the_source_is_left_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    spoil: Any,
    fault: SecretFileFault,
) -> None:
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=spoil)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.cause, report.cleanup) == (
        RenewalOutcome.READ_BACK_FAILED,
        fault,
        Cleanup.RETAINED,
    )
    assert arrangement.directory_holds() == [LOGIN_NAME]
    assert len(arrangement.trees()) == 1


def test_a_home_whose_removal_fails_keeps_the_outcome_and_reports_the_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    a_fake_runner(monkeypatch, on_home=writing(RENEWED_LOGIN))

    with rmtree_refused(monkeypatch):
        report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.cleanup) == (RenewalOutcome.RENEWED, Cleanup.LEFT)
    assert "removal failed" in caplog.text
    assert len(arrangement.trees()) == 1


def test_the_login_command_during_the_hold_is_refused_and_the_renewal_still_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    said: Said,
) -> None:
    installation = an_installation(tmp_path, Provider.OPENAI)
    write_login(installation.login, DUE_ORIGINAL)
    arrangement = an_arrangement(tmp_path, token_file=installation.login)
    stream = Stream(a_codex_login())
    heard: list[tuple[int, str]] = []

    def logging_in(home: Path) -> None:
        writing(RENEWED_LOGIN)(home)
        heard.append(run(installation, stream, said))

    a_fake_runner(monkeypatch, on_home=logging_in)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert heard == [(2, LINE_PREFIX + held_sibling_sentence(installation.sibling) + "\n")]
    assert stream.was_read is False
    assert report.outcome is RenewalOutcome.RENEWED


def test_an_ordinary_run_reads_the_whole_old_login_during_the_hold_and_the_whole_new_one_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    during: list[tuple[bytes | None, list[str]]] = []

    def reading(home: Path) -> None:
        during.append(
            (
                read_codex_login(arrangement.token_file).blanked_document,
                arrangement.directory_holds(),
            )
        )
        writing(RENEWED_LOGIN)(home)

    a_fake_runner(monkeypatch, on_home=reading)

    logged_once(caplog, arrangement.renewer.attempt)

    assert during == [(blanked_copy(DUE_ORIGINAL), arrangement.directory_holds_login_and_sibling())]
    assert read_codex_login(arrangement.token_file).blanked_document == blanked_copy(RENEWED_LOGIN)


def test_a_binary_that_cannot_start_is_not_started_and_everything_is_released(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o644)
    arrangement = an_arrangement(tmp_path, binary=binary)
    held_before = held_pool_slots()

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, report.cleanup) == (RenewalOutcome.NOT_STARTED, Cleanup.REMOVED)
    assert (held_pool_slots(), arrangement.trees()) == (held_before, [])
    assert arrangement.source_is_intact()


def test_no_secret_reaches_a_log_record_a_report_an_argument_or_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(monkeypatch, on_home=writing(RENEWED_LOGIN))

    report = logged_once(caplog, arrangement.renewer.attempt)

    shown = [record.getMessage() for record in caplog.records]
    shown += [repr(report), repr(runner.children[0].arguments)]
    shown += [repr(runner.children[0].environment)]
    assert not any(
        secret in text for secret in the_secrets_of(DUE_ORIGINAL, RENEWED_LOGIN) for text in shown
    )


def test_the_cli_output_never_reaches_a_log_record_or_a_report(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    chatter = "canary-cli-output"
    binary = a_renewing_fake_codex(
        tmp_path, RENEWED_LOGIN, then=f"echo {chatter}; echo {chatter} >&2"
    )
    arrangement = an_arrangement(tmp_path, binary=binary)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert chatter not in caplog.text + repr(report)


@pytest.mark.parametrize("outcome", list(RenewalOutcome))
def test_only_not_due_and_renewed_are_not_failures(outcome: RenewalOutcome) -> None:
    assert outcome.is_failure is (outcome not in (RenewalOutcome.NOT_DUE, RenewalOutcome.RENEWED))


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any directory")
def test_a_root_that_cannot_hold_a_home_fails_the_attempt_and_frees_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = an_arrangement(tmp_path)
    runner = a_fake_runner(monkeypatch)
    held_before = held_pool_slots()
    arrangement.root.chmod(0o500)
    try:
        report = logged_once(caplog, arrangement.renewer.attempt)
    finally:
        arrangement.root.chmod(0o700)

    assert (report.outcome, report.cleanup, runner.calls) == (
        RenewalOutcome.HOME_NOT_CREATED,
        None,
        0,
    )
    assert (held_pool_slots(), arrangement.source_is_intact()) == (held_before, True)


@pytest.mark.parametrize("settled_after_the_child", [False, True])
def test_a_sibling_that_cannot_be_removed_is_named_in_the_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    settled_after_the_child: bool,
) -> None:
    source = (
        DUE_ORIGINAL if settled_after_the_child else a_codex_login(tokens={"refresh_token": ""})
    )
    arrangement = an_arrangement(tmp_path, source)
    a_fake_runner(monkeypatch)
    real_unlink = os.unlink

    def unlinking(path: Any, *rest: Any, **keywords: Any) -> None:
        if Path(path) == arrangement.sibling:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        real_unlink(path, *rest, **keywords)

    monkeypatch.setattr(os, "unlink", unlinking)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.refusal
    assert "is not confirmed gone" in report.refusal
    assert arrangement.sibling.exists()
