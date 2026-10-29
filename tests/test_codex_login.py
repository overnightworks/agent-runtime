"""The one decider of whether a Codex login can start a child."""

from __future__ import annotations

import copy
import json
import pickle
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.codex_support import a_codex_access_token
from tests.support.provider_test_support import (
    A_CODEX_LOGIN_WITH_INVALID_UTF8,
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    a_codex_login,
)

from agent_runtime.codex_login import (
    CodexLogin,
    CodexLoginVerdict,
    CodexRenewalRefusal,
    CodexTiming,
    RenewableCodexLogin,
    UnreadableTiming,
    assess_codex_login,
    made_due_copy,
    read_codex_timing,
    renewable_codex_login,
)

A_DEEPLY_NESTED_DOCUMENT = b"[" * 100_000
A_DOCUMENT_WITH_A_HUGE_INTEGER = b'{"tokens": {"account_id": ' + b"9" * 5_000 + b"}}"

USABLE, INCOMPLETE, MALFORMED = (
    CodexLoginVerdict.USABLE, CodexLoginVerdict.INCOMPLETE, CodexLoginVerdict.MALFORMED,
)


@pytest.mark.parametrize(
    ("document", "verdict"),
    [
        pytest.param(a_codex_login(), USABLE, id="complete"),
        pytest.param(A_CODEX_LOGIN_WITH_INVALID_UTF8, MALFORMED, id="invalid-encoding"),
        pytest.param(A_DEEPLY_NESTED_DOCUMENT, MALFORMED, id="excessive-nesting"),
        pytest.param(A_DOCUMENT_WITH_A_HUGE_INTEGER, MALFORMED, id="integer-over-the-digit-limit"),
        pytest.param(b"not json", MALFORMED, id="bad-syntax"),
        pytest.param(b"[]", MALFORMED, id="top-level-not-an-object"),
        pytest.param(
            b'{"auth_mode":"apikey","OPENAI_API_KEY":"sk-synthetic"}', INCOMPLETE,
            id="api-key-only",
        ),
        pytest.param(b'{"tokens":null}', MALFORMED, id="tokens-null"),
        pytest.param(b'{"tokens":[]}', MALFORMED, id="tokens-not-an-object"),
        pytest.param(b'{"auth_mode":"chatgpt","tokens":{}}', INCOMPLETE, id="empty-tokens"),
        pytest.param(a_codex_login(tokens={"id_token": ""}), INCOMPLETE, id="empty-id-token"),
        pytest.param(a_codex_login(auth_mode=""), INCOMPLETE, id="empty-auth-mode"),
        pytest.param(a_codex_login(last_refresh=None), MALFORMED, id="null-last-refresh"),
        pytest.param(a_codex_login(tokens={"account_id": 7}), MALFORMED, id="numeric-account-id"),
        pytest.param(b'{"tokens":{"account_id":null}}', MALFORMED, id="null-account-id"),
    ],
)
def test_assess_codex_login_names_each_document_shape(
    document: bytes, verdict: CodexLoginVerdict,
) -> None:
    assert assess_codex_login(document).verdict is verdict


def test_a_usable_login_hands_a_child_the_login_without_its_refresh_token() -> None:
    blanked = assess_codex_login(a_codex_login()).blanked_document

    assert blanked == json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "last_refresh": "2026-09-04T19:20:00Z",
        "tokens": {
            "id_token": CANARY_ID_TOKEN,
            "access_token": CANARY_ACCESS_TOKEN,
            "account_id": "account",
            "refresh_token": "",
        },
    }).encode()


@pytest.mark.parametrize("document", [b"not json", b'{"tokens":{}}', a_codex_login(last_refresh=3)])
def test_a_login_that_is_not_usable_carries_no_document(document: bytes) -> None:
    assert assess_codex_login(document).blanked_document is None


@pytest.mark.parametrize(
    "disclose",
    [
        pytest.param(repr, id="repr"),
        pytest.param(str, id="str"),
        pytest.param(pickle.dumps, id="pickle"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
    ],
)
def test_a_usable_codex_login_never_shows_its_tokens(disclose) -> None:
    login = assess_codex_login(a_codex_login())
    assert isinstance(login, CodexLogin)

    if disclose in (repr, str):
        shown = disclose(login)
        assert CANARY_ACCESS_TOKEN not in shown
        assert CANARY_ID_TOKEN not in shown
    else:
        with pytest.raises(TypeError):
            disclose(login)


NOW = 1_800_000_000
LAST_REFRESH_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
THIRTY_DAYS = 30 * 24 * 3600
MAX_CLAIM = 2**53 - 1
READABLE_LIFETIME = {"iat": NOW - 100, "exp": NOW + 100}


def a_login_with_claims(claims: dict[str, object] | bytes) -> bytes:
    return a_codex_login(tokens={"access_token": a_codex_access_token(claims)})


def a_login_with_access_token(access_token: object) -> bytes:
    return a_codex_login(tokens={"access_token": access_token})


def timing_of(claims: dict[str, object] | bytes, now: int = NOW) -> CodexTiming | UnreadableTiming:
    return read_codex_timing(a_login_with_claims(claims), now)


def test_integer_claims_in_range_are_the_timing() -> None:
    assert timing_of({"iat": NOW - 100, "exp": NOW + 100}) == CodexTiming(NOW - 100, NOW + 100)


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"exp": NOW + 1}, id="no-iat"),
        pytest.param({"iat": NOW}, id="no-exp"),
        pytest.param({}, id="neither"),
    ],
)
def test_a_missing_claim_is_named(claims: dict[str, object]) -> None:
    assert timing_of(claims) is UnreadableTiming.CLAIM_MISSING


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(1800000000.0, id="float"),
        pytest.param(1e9, id="exponent-float"),
        pytest.param("1800000000", id="string"),
        pytest.param(None, id="null"),
    ],
)
@pytest.mark.parametrize("which", ["iat", "exp"])
def test_a_claim_that_is_not_a_json_integer_is_wrong_type(which: str, bad: object) -> None:
    claims = {"iat": NOW - 100, "exp": NOW + 100, which: bad}

    assert timing_of(claims) is UnreadableTiming.WRONG_TYPE


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        pytest.param({"iat": 1, "exp": 2}, CodexTiming(1, 2), id="iat-one"),
        pytest.param({"iat": 0, "exp": 5}, UnreadableTiming.OUT_OF_RANGE, id="iat-zero"),
        pytest.param({"iat": -1, "exp": 5}, UnreadableTiming.OUT_OF_RANGE, id="iat-negative"),
        pytest.param(
            {"iat": NOW, "exp": MAX_CLAIM}, CodexTiming(NOW, MAX_CLAIM), id="exp-at-the-top",
        ),
        pytest.param(
            {"iat": NOW, "exp": MAX_CLAIM + 1}, UnreadableTiming.OUT_OF_RANGE, id="exp-over-top",
        ),
    ],
)
def test_claims_must_lie_between_one_and_two_to_the_53_minus_one(
    claims: dict[str, object], expected: CodexTiming | UnreadableTiming
) -> None:
    assert timing_of(claims) == expected


def test_an_exp_of_4000_digits_is_out_of_range_and_one_of_5000_digits_is_not_a_token() -> None:
    four_thousand = b'{"iat": 5, "exp": ' + b"9" * 4_000 + b"}"
    five_thousand = b'{"iat": 5, "exp": ' + b"9" * 5_000 + b"}"

    assert timing_of(four_thousand) is UnreadableTiming.OUT_OF_RANGE
    assert timing_of(five_thousand) is UnreadableTiming.NOT_A_TOKEN


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        pytest.param({"iat": 50, "exp": 50}, UnreadableTiming.EXPIRY_NOT_AFTER_ISSUE, id="equal"),
        pytest.param({"iat": 50, "exp": 49}, UnreadableTiming.EXPIRY_NOT_AFTER_ISSUE, id="before"),
        pytest.param({"iat": 50, "exp": 51}, CodexTiming(50, 51), id="one-second-after"),
    ],
)
def test_expiry_must_be_after_issue(
    claims: dict[str, object], expected: CodexTiming | UnreadableTiming
) -> None:
    assert timing_of(claims, now=60) == expected


def test_issue_up_to_300_seconds_ahead_of_now_is_readable_and_beyond_is_future() -> None:
    assert timing_of({"iat": NOW + 300, "exp": NOW + 900}) == CodexTiming(NOW + 300, NOW + 900)
    assert timing_of({"iat": NOW + 301, "exp": NOW + 900}) is UnreadableTiming.ISSUED_IN_THE_FUTURE


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        pytest.param({"exp": 1e9}, UnreadableTiming.CLAIM_MISSING, id="missing-before-type"),
        pytest.param(
            {"iat": True, "exp": 2**53}, UnreadableTiming.WRONG_TYPE, id="type-before-range",
        ),
        pytest.param(
            {"iat": NOW + 1000, "exp": NOW + 10},
            UnreadableTiming.EXPIRY_NOT_AFTER_ISSUE,
            id="order-before-future",
        ),
    ],
)
def test_the_causes_are_checked_in_the_stated_precedence(
    claims: dict[str, object], expected: UnreadableTiming
) -> None:
    assert timing_of(claims) is expected


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b'{"tokens":{"access_token":"x"}}\xff', id="invalid-utf8"),
        pytest.param(b"[" * 100_000, id="nested"),
        pytest.param(b"[]", id="array"),
        pytest.param(b'{"tokens":[]}', id="tokens-not-an-object"),
        pytest.param(b'{"tokens":{}}', id="no-access-token"),
        pytest.param(a_login_with_access_token(""), id="empty-access-token"),
        pytest.param(a_login_with_access_token(7), id="numeric-access-token"),
    ],
)
def test_a_document_without_an_access_token_is_named_and_never_raises(document: bytes) -> None:
    assert read_codex_timing(document, NOW) is UnreadableTiming.NO_ACCESS_TOKEN


@pytest.mark.parametrize(
    "access_token",
    [
        pytest.param("a.b", id="two-segments"),
        pytest.param("a.b.c.d", id="four-segments"),
        pytest.param("e30.@@@.sig", id="not-base64url"),
        pytest.param("e30.e30=.sig", id="padded"),
        pytest.param("e30.YQ.sig", id="not-a-json-object"),
        pytest.param("e30.//8.sig", id="standard-alphabet"),
        pytest.param(a_codex_access_token(b"\xff\xfe"), id="not-utf8"),
        pytest.param(a_codex_access_token(b'["iat"]'), id="array-payload"),
        pytest.param(a_codex_access_token(b"[" * 100_000), id="nested-payload"),
    ],
)
def test_an_access_token_whose_claims_cannot_be_read_is_named(access_token: str) -> None:
    assert read_codex_timing(a_login_with_access_token(access_token), NOW) is (
        UnreadableTiming.NOT_A_TOKEN
    )


@pytest.mark.parametrize(
    ("now", "due"),
    [
        pytest.param(1_000 + 50, True, id="at-half"),
        pytest.param(1_000 + 49, False, id="one-second-early"),
    ],
)
def test_a_timing_is_due_at_half_its_lifetime(now: int, due: bool) -> None:
    assert CodexTiming(1_000, 1_100).is_due(now) is due


def test_half_the_lifetime_is_floored_for_an_odd_lifetime() -> None:
    timing = CodexTiming(1_000, 1_101)

    assert (timing.is_due(1_049), timing.is_due(1_050)) == (False, True)


def test_a_240_hour_lifetime_is_due_at_exactly_120_hours() -> None:
    timing = CodexTiming(NOW, NOW + 240 * 3600)

    assert (timing.is_due(NOW + 120 * 3600 - 1), timing.is_due(NOW + 120 * 3600)) == (False, True)


def test_an_opaque_access_token_still_gives_a_usable_login_but_no_timing() -> None:
    document = a_codex_login()

    assert assess_codex_login(document).verdict is USABLE
    assert read_codex_timing(document, NOW) is UnreadableTiming.NOT_A_TOKEN


@pytest.mark.parametrize(
    ("document", "refusal"),
    [
        pytest.param(
            a_codex_login(tokens={"refresh_token": ""}),
            CodexRenewalRefusal.NO_REFRESH_TOKEN, id="empty",
        ),
        pytest.param(
            a_codex_login(tokens={"refresh_token": 7}),
            CodexRenewalRefusal.NO_REFRESH_TOKEN, id="numeric",
        ),
        pytest.param(
            json.dumps({"auth_mode": "chatgpt", "last_refresh": "x", "tokens": {
                "access_token": "a", "id_token": "i", "account_id": "c"}}).encode(),
            CodexRenewalRefusal.NO_REFRESH_TOKEN, id="missing",
        ),
        pytest.param(b"not json", CodexRenewalRefusal.NOT_USABLE, id="malformed"),
        pytest.param(b'{"tokens":{}}', CodexRenewalRefusal.NOT_USABLE, id="incomplete"),
    ],
)
def test_a_document_without_a_refresh_token_or_not_usable_is_refused(
    document: bytes, refusal: CodexRenewalRefusal
) -> None:
    assert renewable_codex_login(document) is refusal


def test_a_full_login_is_renewable_and_keeps_its_document() -> None:
    document = a_codex_login()

    login = renewable_codex_login(document)

    assert isinstance(login, RenewableCodexLogin)
    assert login.document == document


def test_logins_share_a_refresh_token_until_it_rotates() -> None:
    login = renewable_codex_login(a_codex_login())
    same_token_new_access = renewable_codex_login(a_codex_login(tokens={"access_token": "other"}))
    rotated = renewable_codex_login(a_codex_login(tokens={"refresh_token": "rotated"}))
    assert isinstance(login, RenewableCodexLogin)
    assert isinstance(same_token_new_access, RenewableCodexLogin)
    assert isinstance(rotated, RenewableCodexLogin)

    assert login.shares_refresh_token_with(same_token_new_access)
    assert not login.shares_refresh_token_with(rotated)


def a_renewable_login(**tokens: object) -> RenewableCodexLogin:
    document = a_codex_login(tokens=tokens, OPENAI_API_KEY="sk-extra", extra={"k": 1})
    login = renewable_codex_login(document)
    assert isinstance(login, RenewableCodexLogin)
    return login


def test_the_made_due_copy_empties_the_access_token_and_ages_last_refresh_by_30_days() -> None:
    copy_document = json.loads(made_due_copy(a_renewable_login(), NOW))

    aged = datetime.fromtimestamp(NOW, UTC) - timedelta(days=30)
    assert copy_document["tokens"]["access_token"] == ""
    assert copy_document["last_refresh"] == aged.strftime(LAST_REFRESH_FORMAT)
    assert NOW - THIRTY_DAYS == int(aged.timestamp())


def test_the_made_due_copy_changes_nothing_else_and_keeps_member_order() -> None:
    login = a_renewable_login()
    original = json.loads(login.document)
    changed = json.loads(made_due_copy(login, NOW))

    original["tokens"]["access_token"] = ""
    original["last_refresh"] = changed["last_refresh"]
    assert changed == original
    assert list(changed) == list(original)
    assert list(changed["tokens"]) == list(original["tokens"])
    assert changed["tokens"]["refresh_token"] == "canary-refresh-token"
    assert changed["OPENAI_API_KEY"] == "sk-extra"
    assert changed["extra"] == {"k": 1}


def test_the_made_due_copy_has_no_timing_and_leaves_the_login_document_alone() -> None:
    document = a_login_with_claims(READABLE_LIFETIME)
    login = renewable_codex_login(document)
    assert isinstance(login, RenewableCodexLogin)

    copied = made_due_copy(login, NOW)

    assert read_codex_timing(copied, NOW) is UnreadableTiming.NO_ACCESS_TOKEN
    assert login.document == document


@pytest.mark.parametrize(
    "disclose",
    [
        pytest.param(repr, id="repr"),
        pytest.param(str, id="str"),
        pytest.param(pickle.dumps, id="pickle"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
    ],
)
def test_a_renewable_login_never_shows_or_serialises_its_tokens(disclose) -> None:
    login = a_renewable_login()

    if disclose in (repr, str):
        shown = disclose(login)
        assert "canary" not in shown
    else:
        with pytest.raises(TypeError):
            disclose(login)
