"""One renewal attempt of the dedicated Grok login: what the Grok entry shares with the Codex
one and where it differs, driven through the real ``ProviderRenewer``.

Every row runs the renewing stand-in CLI through the real bounded runner, except the unreadable
timing row, which shares its arrangement with the Codex entry and uses the fake runner of
``service_renewal_support``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.support.grok_support import a_renewing_fake_grok, children_started
from tests.support.provider_test_support import a_codex_login
from tests.support.service_login_support import held_swap
from tests.support.service_renewal_support import (  # noqa: F401 - the autouse fixture
    DUE_GROK_LOGIN,
    GROK_HALF_LIFE,
    LOGIN_NAME,
    NOW,
    RENEWED_GROK_LOGIN,
    ROTATED_REFRESH,
    Arrangement,
    RecordingPause,
    a_fake_runner,
    a_grok_login_issued_at,
    a_login_issued_at,
    an_arrangement,
    logged_once,
    pool_filled,
    run_rounds,
    the_held_ends_confirmed,
    writing,
)

from agent_runtime.grok.run import GROK_CLI_PROCESS_POOL
from agent_runtime.grok_login import blanked_copy
from agent_runtime.login_timing import UnreadableTiming
from agent_runtime_service.config import Provider
from agent_runtime_service.login_file import held_sibling_sentence
from agent_runtime_service.renewal import (
    CODEX_RENEWAL,
    GROK_RENEWAL,
    NotRenewedCause,
    RenewalOutcome,
)
from agent_runtime_service.renewal_schedule import RENEWAL_BACKOFF_SECONDS, RenewalSchedule

QUARTER_HOUR_SECONDS = 15 * 60


def a_grok_arrangement(
    tmp_path: Path,
    source: bytes = DUE_GROK_LOGIN,
    *,
    renewed: bytes = RENEWED_GROK_LOGIN,
    rewritten_to: bytes | None = None,
) -> Arrangement:
    """A Grok login under a renewer whose CLI is the stand-in that writes ``renewed``.

    With ``rewritten_to`` the stand-in first writes that document to the login, as another writer.
    """
    login = tmp_path / "login" / LOGIN_NAME
    binary = a_renewing_fake_grok(
        tmp_path,
        source,
        renewed,
        GROK_HALF_LIFE,
        source_rewritten_to=None if rewritten_to is None else (login, rewritten_to),
    )
    return an_arrangement(
        tmp_path, source, binary=binary, entry=GROK_RENEWAL, provider=Provider.XAI
    )


# Matrix 15: a due login is renewed.


def test_a_due_grok_login_is_renewed_over_the_document_read_and_its_home_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = a_grok_arrangement(tmp_path, renewed=RENEWED_GROK_LOGIN)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.RENEWED
    assert arrangement.token_file.read_bytes() == RENEWED_GROK_LOGIN
    assert (arrangement.directory_holds(), arrangement.trees()) == ([LOGIN_NAME], [])


# Matrix 16: a login not yet due starts nothing.


def test_a_grok_login_not_yet_due_starts_no_child_and_leaves_no_sibling(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = a_grok_arrangement(tmp_path, a_grok_login_issued_at(NOW))

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, children_started(tmp_path)) == (RenewalOutcome.NOT_DUE, 0)
    assert arrangement.source_is_intact()
    assert (arrangement.directory_holds(), arrangement.trees()) == ([LOGIN_NAME], [])


# Matrix 17: an unreadable timing is held per entry.

UNREADABLE_TIMING = {
    "grok": (GROK_RENEWAL, Provider.XAI, a_grok_login_issued_at(0, access_key="opaque-key"), 2),
    "codex": (CODEX_RENEWAL, Provider.OPENAI, a_codex_login(), 1),
}


@pytest.mark.parametrize("entry_name", UNREADABLE_TIMING)
def test_an_unreadable_timing_renews_again_after_a_quarter_hour_only_where_the_entry_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    entry_name: str,
) -> None:
    entry, provider, source, children_after_two_attempts = UNREADABLE_TIMING[entry_name]
    arrangement = an_arrangement(tmp_path, source, entry=entry, provider=provider)
    runner = a_fake_runner(monkeypatch)

    first = logged_once(caplog, arrangement.renewer.attempt)
    arrangement.clocks.monotonic = QUARTER_HOUR_SECONDS
    second = logged_once(caplog, arrangement.renewer.attempt)

    assert first.outcome is RenewalOutcome.REFRESH_FAILED
    assert first.source_timing in set(UnreadableTiming)
    assert runner.calls == children_after_two_attempts
    assert (second.outcome is RenewalOutcome.NOT_DUE) is (children_after_two_attempts == 1)


# Matrix 18: a result that is committed but not "renewed" backs the round off.

WRITTEN_NOT_RENEWED = {
    "refresh-token-unchanged": (
        a_grok_login_issued_at(NOW),
        NotRenewedCause.REFRESH_TOKEN_UNCHANGED,
    ),
    "timing-unreadable": (
        a_grok_login_issued_at(0, refresh_token=ROTATED_REFRESH, access_key="opaque-key"),
        NotRenewedCause.TIMING_UNREADABLE,
    ),
}


@pytest.mark.parametrize("case", WRITTEN_NOT_RENEWED)
def test_a_result_that_is_written_but_not_renewed_is_committed_and_the_round_backs_off_an_hour(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    result, cause = WRITTEN_NOT_RENEWED[case]
    arrangement = a_grok_arrangement(tmp_path, renewed=result)
    pause = RecordingPause(1)
    caplog.set_level("DEBUG")

    run_rounds(RenewalSchedule({Provider.XAI: arrangement.renewer}, pause=pause), pause)

    assert arrangement.token_file.read_bytes() == result
    assert f"{RenewalOutcome.WRITTEN_NOT_RENEWED.value} (cause: {cause.value}" in caplog.text
    assert pause.seconds == [RENEWAL_BACKOFF_SECONDS]


# Matrix 19: a copy the CLI left or blanked is never committed.

NOT_COMMITTED = {
    "copy-left-as-it-was": (DUE_GROK_LOGIN, RenewalOutcome.REFRESH_FAILED),
    "blanked-result": (blanked_copy(DUE_GROK_LOGIN), RenewalOutcome.MALFORMED_RESULT),
}


@pytest.mark.parametrize("case", NOT_COMMITTED)
def test_a_copy_the_cli_left_or_blanked_leaves_the_source_intact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    result, outcome = NOT_COMMITTED[case]
    assert result is not None
    arrangement = a_grok_arrangement(tmp_path, renewed=result)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is outcome
    assert arrangement.source_is_intact()
    assert arrangement.trees() == []


# Matrix 20: another writer, a held sibling and a directory that cannot hold one.


def test_a_source_rewritten_while_the_child_runs_keeps_its_newer_document(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    newer = a_grok_login_issued_at(NOW - 5, refresh_token="newer-refresh")
    arrangement = a_grok_arrangement(tmp_path, rewritten_to=newer)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.SOURCE_CHANGED
    assert arrangement.token_file.read_bytes() == newer
    assert (arrangement.directory_holds(), arrangement.trees()) == ([LOGIN_NAME], [])


def test_a_sibling_held_by_another_writer_blocks_a_grok_attempt_and_is_left_as_it_was(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = a_grok_arrangement(tmp_path)
    holder = held_swap(arrangement.token_file)
    held = os.lstat(arrangement.sibling)

    report = logged_once(caplog, arrangement.renewer.attempt)

    assert report.outcome is RenewalOutcome.SIBLING_HELD
    assert report.refusal == held_sibling_sentence(arrangement.sibling)
    assert (os.lstat(arrangement.sibling), children_started(tmp_path)) == (held, 0)
    holder.abandon()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any directory")
def test_a_directory_that_cannot_hold_a_sibling_leaves_the_grok_source_intact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = a_grok_arrangement(tmp_path)
    arrangement.token_file.parent.chmod(0o500)
    try:
        report = logged_once(caplog, arrangement.renewer.attempt)
    finally:
        arrangement.token_file.parent.chmod(0o700)

    assert (report.outcome, children_started(tmp_path)) == (RenewalOutcome.RESERVE_REFUSED, 0)
    assert arrangement.source_is_intact()


# Matrix 21: a full Grok pool starts no child.


def test_a_full_grok_pool_fails_the_attempt_without_a_child(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    arrangement = a_grok_arrangement(tmp_path)

    with pool_filled(GROK_CLI_PROCESS_POOL):
        report = logged_once(caplog, arrangement.renewer.attempt)

    assert (report.outcome, children_started(tmp_path)) == (RenewalOutcome.POOL_FULL, 0)
    assert (arrangement.source_is_intact(), arrangement.trees()) == (True, [])
