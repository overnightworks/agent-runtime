"""Where a login stands for its renewal, and what ``agent-runtime-service-login --check`` makes of
it: the pure precedence at its boundaries, then the command through ``main`` with logins issued
relative to the real clock, never near a boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.support.codex_support import a_codex_access_token
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.provider_test_support import a_codex_login
from tests.support.service_login_support import (  # noqa: F401 - the fixtures are named in their decorators
    CANARY_FOREIGN,
    CANARY_REFRESH,
    CODEX_FILE_REFUSAL,
    LINE_PREFIX,
    Installation,
    Said,
    Stream,
    an_installation,
    an_old_file_fails,
    held_swap,
    line_of,
    run,
    the_signal_handlers_restored,
    what_was_said,
)
from tests.support.service_renewal_support import (
    HALF_LIFE,
    LIFETIME,
    NOW,
    a_fake_runner,
    a_renewed_login_now,
    an_arrangement,
    the_secrets_of,
    writing,
)
from tests.support.service_table_support import write_login

import agent_runtime_service.config as service_config
from agent_runtime.codex_login import UnreadableTiming
from agent_runtime_service.config import Provider
from agent_runtime_service.login import (
    EXIT_EXPIRY_UNKNOWN,
    EXIT_REMAINS,
    EXIT_SIGN_IN_AGAIN,
)
from agent_runtime_service.login_file import held_sibling_sentence
from agent_runtime_service.login_standing import (
    RENEWAL_WINDOW_SECONDS,
    LoginStanding,
    Standing,
    login_standing,
)
from agent_runtime_service.renewal import CODEX_RENEWAL, RenewalOutcome
from agent_runtime_service.renewal_schedule import RENEWAL_BACKOFF_SECONDS, RENEWAL_INTERVAL_SECONDS

CANARY_ACCOUNT = "canary-account-id"
MARGIN_SECONDS = 60
ACCEPTED = "accepted by the service's reader; whether the provider still accepts it shows only"
THE_YEAR_2100 = 4_102_444_800
THE_LARGEST_EXPIRY = 2**53 - 1
DUE = NOW + HALF_LIFE
EXPIRY = NOW + LIFETIME


def a_login_at(
    issued_at: int, *, lifetime: int = LIFETIME, refresh_token: str = CANARY_REFRESH
) -> bytes:
    claims = {"iat": issued_at, "exp": issued_at + lifetime}
    return a_codex_login(
        tokens={
            "access_token": a_codex_access_token(claims),
            "refresh_token": refresh_token,
            "account_id": CANARY_ACCOUNT,
        }
    )


def standing_at(now: int, document: bytes | None = None) -> Standing:
    return login_standing(document or a_login_at(NOW), now, CODEX_RENEWAL)


@pytest.mark.parametrize(
    ("now", "state"),
    [
        (DUE - 1, LoginStanding.USABLE),
        (DUE, LoginStanding.DUE),
        (DUE + RENEWAL_INTERVAL_SECONDS + RENEWAL_BACKOFF_SECONDS, LoginStanding.DUE),
        (DUE + RENEWAL_WINDOW_SECONDS - 1, LoginStanding.DUE),
        (DUE + RENEWAL_WINDOW_SECONDS, LoginStanding.NOT_RENEWED_IN_TIME),
        (EXPIRY - 1, LoginStanding.NOT_RENEWED_IN_TIME),
        (EXPIRY, LoginStanding.EXPIRED),
    ],
    ids=[
        "just-before-due",
        "due",
        "one-failed-attempt-and-its-retry",
        "just-inside-the-window",
        "window-over",
        "just-before-expiry",
        "expired",
    ],
)
def test_a_login_stands_by_how_long_it_has_been_due(now: int, state: LoginStanding) -> None:
    assert standing_at(now) == Standing(state, EXPIRY)


def test_expiry_precedes_overdue_when_the_lifetime_is_the_window() -> None:
    document = a_login_at(NOW, lifetime=RENEWAL_WINDOW_SECONDS)

    assert standing_at(NOW + RENEWAL_WINDOW_SECONDS, document).state is LoginStanding.EXPIRED


@pytest.mark.parametrize(
    ("claims", "now", "cause"),
    [
        (b"opaque", NOW, UnreadableTiming.NOT_A_TOKEN),
        ({"iat": NOW}, NOW, UnreadableTiming.CLAIM_MISSING),
        ({"iat": NOW + 301, "exp": NOW + LIFETIME}, NOW, UnreadableTiming.ISSUED_IN_THE_FUTURE),
    ],
    ids=["not-a-token", "no-exp", "issued-in-the-future"],
)
def test_unreadable_timing_is_reported_with_its_closed_cause(
    claims: dict[str, int] | bytes, now: int, cause: UnreadableTiming
) -> None:
    token = a_codex_access_token(claims) if isinstance(claims, dict) else "not.a.jwt"
    document = a_codex_login(tokens={"access_token": token})

    assert standing_at(now, document) == Standing(LoginStanding.EXPIRY_UNKNOWN, None, cause)


@pytest.mark.parametrize("now", [DUE - 1, EXPIRY + 1], ids=["not-due", "expired"])
def test_a_login_without_a_refresh_token_asks_for_a_sign_in_at_once(now: int) -> None:
    document = a_login_at(NOW, refresh_token="")

    assert standing_at(now, document).state is LoginStanding.NOT_RENEWABLE


def test_an_api_key_document_is_not_a_usable_login() -> None:
    document = json.dumps({"OPENAI_API_KEY": CANARY_FOREIGN}).encode()

    assert standing_at(NOW, document).state is LoginStanding.NOT_USABLE


def test_the_largest_expiry_is_judged_without_raising() -> None:
    document = a_login_at(NOW, lifetime=THE_LARGEST_EXPIRY - NOW)

    assert standing_at(NOW, document) == Standing(LoginStanding.USABLE, THE_LARGEST_EXPIRY)


def test_exactly_the_sign_in_verdicts_ask_for_a_sign_in() -> None:
    asking = {state for state in LoginStanding if state.asks_for_sign_in}

    assert asking == {
        LoginStanding.NOT_USABLE,
        LoginStanding.NOT_RENEWABLE,
        LoginStanding.EXPIRED,
        LoginStanding.NOT_RENEWED_IN_TIME,
    }


def test_no_standing_holds_a_token() -> None:
    document = a_login_at(NOW)

    shown = repr(standing_at(DUE, document))

    assert not any(secret in shown for secret in the_secrets_of(document))


# The command: the standing through ``main``.


def an_openai_login(tmp_path: Path, document: bytes) -> Installation:
    installation = an_installation(tmp_path, Provider.OPENAI)
    write_login(installation.login, document)
    return installation


def checked(installation: Installation, said: Said) -> tuple[int, str]:
    """``--check`` reads no input and changes nothing: the file, its inode, the directory."""
    stream = Stream(b"never read")
    before = installation.snapshot()

    status, heard = run(installation, stream, said, "--check")

    assert not stream.was_read
    assert installation.snapshot() == before
    assert not any(secret in heard for secret in secrets_in(installation.login.read_bytes()))
    return status, heard


def secrets_in(document: bytes) -> tuple[str, ...]:
    """The tokens of a Codex login, else the whole document: neither may be printed."""
    try:
        return the_secrets_of(document)
    except (KeyError, ValueError):
        return (document.decode(errors="replace").strip(),)


def issued_ago(seconds: int, **keywords: Any) -> bytes:
    return a_login_at(int(time.time()) - seconds, **keywords)


def expiry_of(heard: str) -> str:
    return heard.rsplit("expires at ", 1)[1].split(";")[0].strip()


def test_a_login_issued_a_minute_ago_is_not_due_and_gives_its_expiry_in_utc(
    tmp_path: Path, said: Said
) -> None:
    issued = int(time.time()) - MARGIN_SECONDS
    installation = an_openai_login(tmp_path, a_login_at(issued))

    status, heard = checked(installation, said)

    assert status == 0
    assert heard.startswith(line_of(installation, ACCEPTED))
    expires = datetime.fromtimestamp(issued + LIFETIME, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f"{LoginStanding.USABLE}; it expires at {expires}\n" in heard


def test_an_expiry_in_the_year_2100_is_written_in_full(tmp_path: Path, said: Said) -> None:
    now = int(time.time())
    document = a_login_at(now - MARGIN_SECONDS, lifetime=THE_YEAR_2100 - now + MARGIN_SECONDS)
    installation = an_openai_login(tmp_path, document)

    status, heard = checked(installation, said)

    assert status == 0
    assert f"{LoginStanding.USABLE}; it expires at 2100-01-01T00:00:00Z" in heard


def test_an_expiry_beyond_the_calendar_is_written_as_epoch_seconds(
    tmp_path: Path, said: Said
) -> None:
    now = int(time.time())
    document = a_login_at(now - MARGIN_SECONDS, lifetime=THE_LARGEST_EXPIRY - now + MARGIN_SECONDS)
    installation = an_openai_login(tmp_path, document)

    status, heard = checked(installation, said)

    assert status == 0
    assert f"it expires at {THE_LARGEST_EXPIRY}\n" in heard


def test_a_login_due_for_a_minute_is_still_fine_because_a_running_service_renews_it(
    tmp_path: Path, said: Said
) -> None:
    installation = an_openai_login(tmp_path, issued_ago(HALF_LIFE + MARGIN_SECONDS))

    status, heard = checked(installation, said)

    assert status == 0
    assert f"{LoginStanding.DUE}, which a running service makes within " in heard
    assert f"within {RENEWAL_WINDOW_SECONDS // 60} minutes" in heard


def test_a_login_due_beyond_the_window_asks_for_a_sign_in(tmp_path: Path, said: Said) -> None:
    installation = an_openai_login(
        tmp_path, issued_ago(HALF_LIFE + RENEWAL_WINDOW_SECONDS + MARGIN_SECONDS)
    )

    status, heard = checked(installation, said)

    assert status == EXIT_SIGN_IN_AGAIN
    assert f"due for renewal for {RENEWAL_WINDOW_SECONDS // 60} minutes or more" in heard
    assert "not renewed" in heard
    assert "sign in again" in heard


def test_an_expired_login_asks_for_a_sign_in(tmp_path: Path, said: Said) -> None:
    installation = an_openai_login(tmp_path, issued_ago(LIFETIME + MARGIN_SECONDS))

    status, heard = checked(installation, said)

    assert status == EXIT_SIGN_IN_AGAIN
    assert f"{LoginStanding.EXPIRED} at " in heard
    assert "sign in again" in heard


def test_an_opaque_access_token_gives_the_expiry_exit_and_its_cause(
    tmp_path: Path, said: Said
) -> None:
    installation = an_installation(tmp_path, Provider.OPENAI)

    status, heard = checked(installation, said)

    assert status == EXIT_EXPIRY_UNKNOWN
    assert f"{LoginStanding.EXPIRY_UNKNOWN}: {UnreadableTiming.NOT_A_TOKEN}" in heard
    assert "once a day" in heard


def test_a_login_without_a_refresh_token_asks_for_a_sign_in_before_it_is_due(
    tmp_path: Path, said: Said
) -> None:
    installation = an_openai_login(tmp_path, issued_ago(MARGIN_SECONDS, refresh_token=""))

    status, heard = checked(installation, said)

    assert status == EXIT_SIGN_IN_AGAIN
    assert f"{LoginStanding.NOT_RENEWABLE}, so the service cannot renew it" in heard


# A sibling beside the login: only a sign-in verdict is turned into exit 3.


def overdue_login() -> bytes:
    return issued_ago(HALF_LIFE + RENEWAL_WINDOW_SECONDS + MARGIN_SECONDS)


def test_an_overdue_login_with_a_held_sibling_gives_three_and_leaves_the_holders_sibling(
    tmp_path: Path, said: Said
) -> None:
    installation = an_openai_login(tmp_path, overdue_login())
    holder = held_swap(installation.login)
    held = os.lstat(installation.sibling)

    status, heard = checked(installation, said)

    assert status == EXIT_REMAINS
    standing, recovery = heard.splitlines()[1:]
    assert "not renewed" in standing
    assert recovery == LINE_PREFIX + held_sibling_sentence(installation.sibling)
    assert os.lstat(installation.sibling) == held
    holder.abandon()


@pytest.mark.parametrize(
    "document",
    [
        issued_ago(LIFETIME + MARGIN_SECONDS),
        json.dumps({"OPENAI_API_KEY": CANARY_FOREIGN}).encode(),
    ],
    ids=["expired", "refused"],
)
def test_every_sign_in_verdict_with_a_held_sibling_gives_three(
    tmp_path: Path, said: Said, document: bytes
) -> None:
    installation = an_installation(tmp_path, Provider.OPENAI)
    write_login(installation.login, document)
    holder = held_swap(installation.login)

    status, heard = run(installation, Stream(b""), said, "--check")

    assert status == EXIT_REMAINS
    assert heard.endswith(LINE_PREFIX + held_sibling_sentence(installation.sibling) + "\n")
    holder.abandon()


@pytest.mark.parametrize(
    "document",
    [issued_ago(MARGIN_SECONDS), issued_ago(HALF_LIFE + MARGIN_SECONDS)],
    ids=["usable", "due"],
)
def test_a_sibling_beside_a_usable_or_due_login_is_not_mentioned(
    tmp_path: Path, said: Said, document: bytes
) -> None:
    installation = an_openai_login(tmp_path, document)
    holder = held_swap(installation.login)
    held = os.lstat(installation.sibling)

    status, heard = checked(installation, said)

    assert status == 0
    assert held_sibling_sentence(installation.sibling) not in heard
    assert os.lstat(installation.sibling) == held
    holder.abandon()


def test_a_sibling_that_cannot_be_inspected_counts_as_present(
    tmp_path: Path, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = an_openai_login(tmp_path, overdue_login())
    an_old_file_fails(monkeypatch, "lstat", installation.sibling)

    status, _ = checked(installation, said)

    assert status == EXIT_REMAINS


@pytest.mark.parametrize("with_sibling", [False, True], ids=["alone", "with-sibling"])
def test_a_provider_without_a_renewal_gets_only_the_readers_verdict(
    tmp_path: Path, said: Said, with_sibling: bool
) -> None:
    installation = an_installation(tmp_path, Provider.ANTHROPIC)
    holder = held_swap(installation.login) if with_sibling else None

    status, heard = checked(installation, said)

    assert status == 0
    assert heard == line_of(installation, f"{ACCEPTED} in a run") + "\n"
    if holder is not None:
        holder.abandon()


# Two reads, never a mix.


def swapped_right_after_accepting(
    monkeypatch: pytest.MonkeyPatch, installation: Installation, replacement: bytes | None
) -> None:
    real = service_config.LOGIN_READERS[installation.provider]

    def reading(path: Path) -> object:
        accepted = real(path)
        if replacement is None:
            path.unlink()
        else:
            write_login(path, replacement)
        return accepted

    monkeypatch.setitem(service_config.LOGIN_READERS, installation.provider, reading)


@pytest.mark.parametrize(
    ("newer", "exit_status", "said_so"),
    [
        (issued_ago(MARGIN_SECONDS), 0, str(LoginStanding.USABLE)),
        (issued_ago(LIFETIME + MARGIN_SECONDS), EXIT_SIGN_IN_AGAIN, str(LoginStanding.EXPIRED)),
    ],
    ids=["overdue-then-fresh", "fresh-then-expired"],
)
def test_the_standing_describes_the_document_of_the_second_read(
    tmp_path: Path,
    said: Said,
    monkeypatch: pytest.MonkeyPatch,
    newer: bytes,
    exit_status: int,
    said_so: str,
) -> None:
    older = overdue_login() if exit_status == 0 else issued_ago(MARGIN_SECONDS)
    installation = an_openai_login(tmp_path, older)
    swapped_right_after_accepting(monkeypatch, installation, newer)

    with said.watching():
        status, heard = run(installation, Stream(b""), said, "--check")

    assert status == exit_status
    assert said_so in heard
    assert "not renewed" not in heard


def test_a_file_removed_after_the_reader_accepted_it_asks_for_a_sign_in(
    tmp_path: Path, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = an_openai_login(tmp_path, issued_ago(MARGIN_SECONDS))
    swapped_right_after_accepting(monkeypatch, installation, None)

    status, heard = run(installation, Stream(b""), said, "--check")

    assert status == EXIT_SIGN_IN_AGAIN
    assert "cannot be read: No such file or directory" in heard


def test_a_refused_file_gives_the_readers_text_and_asks_for_a_sign_in(
    tmp_path: Path, said: Said
) -> None:
    document = json.dumps({"OPENAI_API_KEY": CANARY_FOREIGN}).encode()
    installation = an_installation(tmp_path, Provider.OPENAI)
    write_login(installation.login, document)

    status, heard = checked(installation, said)

    assert status == EXIT_SIGN_IN_AGAIN
    assert CODEX_FILE_REFUSAL in heard


# What a host's setup reads around one renewal.


def test_the_command_shows_a_renewal_as_a_sign_in_request_turned_into_a_usable_login(
    tmp_path: Path, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = an_installation(tmp_path, Provider.OPENAI)
    write_login(installation.login, overdue_login())
    arrangement = an_arrangement(tmp_path, token_file=installation.login)
    arrangement.clocks.wall = int(time.time())
    a_fake_runner(monkeypatch, on_home=writing(a_renewed_login_now()))
    before_status, before = checked(installation, said)

    report = arrangement.renewer.attempt()
    after_status, after = checked(installation, said)

    assert before_status == EXIT_SIGN_IN_AGAIN
    assert report.outcome is RenewalOutcome.RENEWED
    assert after_status == 0
    assert expiry_of(after) > expiry_of(before)


def test_the_standing_module_is_not_imported_when_the_command_module_is() -> None:
    probe = (
        "import sys, agent_runtime_service.login;"
        " sys.exit('agent_runtime_service.login_standing' in sys.modules)"
    )

    finished = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, check=False)

    assert finished.returncode == 0
