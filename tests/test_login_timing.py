"""The one reader of when a login's access token was issued and when it expires."""

from __future__ import annotations

import pytest
from tests.support.codex_support import a_codex_access_token

from agent_runtime.login_timing import AccessTokenTiming, UnreadableTiming, read_access_token_timing

NOW = 1_800_000_000
MAX_CLAIM = 2**53 - 1


def timing_of(
    claims: dict[str, object] | bytes, now: int = NOW
) -> AccessTokenTiming | UnreadableTiming:
    return read_access_token_timing(a_codex_access_token(claims), now)


def test_integer_claims_in_range_are_the_timing() -> None:
    assert timing_of({"iat": NOW - 100, "exp": NOW + 100}) == AccessTokenTiming(
        NOW - 100, NOW + 100
    )


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
        pytest.param("true", id="true"),
        pytest.param("false", id="false"),
        pytest.param("1800000000.0", id="float"),
        pytest.param("1e9", id="exponent-float"),
        pytest.param('"1800000000"', id="string"),
        pytest.param("null", id="null"),
    ],
)
@pytest.mark.parametrize("which", ["iat", "exp"])
def test_a_claim_that_is_not_a_json_integer_is_wrong_type(which: str, bad: str) -> None:
    values = {"iat": str(NOW - 100), "exp": str(NOW + 100), which: bad}
    claims = ("{" + ", ".join(f'"{name}": {text}' for name, text in values.items()) + "}").encode()

    assert timing_of(claims) is UnreadableTiming.WRONG_TYPE


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        pytest.param({"iat": 1, "exp": 2}, AccessTokenTiming(1, 2), id="iat-one"),
        pytest.param({"iat": 0, "exp": 5}, UnreadableTiming.OUT_OF_RANGE, id="iat-zero"),
        pytest.param({"iat": -1, "exp": 5}, UnreadableTiming.OUT_OF_RANGE, id="iat-negative"),
        pytest.param(
            {"iat": NOW, "exp": MAX_CLAIM},
            AccessTokenTiming(NOW, MAX_CLAIM),
            id="exp-at-the-top",
        ),
        pytest.param(
            {"iat": NOW, "exp": MAX_CLAIM + 1},
            UnreadableTiming.OUT_OF_RANGE,
            id="exp-over-top",
        ),
    ],
)
def test_claims_must_lie_between_one_and_two_to_the_53_minus_one(
    claims: dict[str, object], expected: AccessTokenTiming | UnreadableTiming
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
        pytest.param({"iat": 50, "exp": 51}, AccessTokenTiming(50, 51), id="one-second-after"),
    ],
)
def test_expiry_must_be_after_issue(
    claims: dict[str, object], expected: AccessTokenTiming | UnreadableTiming
) -> None:
    assert timing_of(claims, now=60) == expected


def test_issue_up_to_300_seconds_ahead_of_now_is_readable_and_beyond_is_future() -> None:
    assert timing_of({"iat": NOW + 300, "exp": NOW + 900}) == AccessTokenTiming(
        NOW + 300, NOW + 900
    )
    assert timing_of({"iat": NOW + 301, "exp": NOW + 900}) is UnreadableTiming.ISSUED_IN_THE_FUTURE


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        pytest.param(b'{"exp": 1e9}', UnreadableTiming.CLAIM_MISSING, id="missing-before-type"),
        pytest.param(
            {"iat": True, "exp": 2**53},
            UnreadableTiming.WRONG_TYPE,
            id="type-before-range",
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
    assert read_access_token_timing(access_token, NOW) is UnreadableTiming.NOT_A_TOKEN


@pytest.mark.parametrize(
    ("now", "due"),
    [
        pytest.param(1_000 + 50, True, id="at-half"),
        pytest.param(1_000 + 49, False, id="one-second-early"),
    ],
)
def test_a_timing_is_due_at_half_its_lifetime(now: int, due: bool) -> None:
    assert AccessTokenTiming(1_000, 1_100).is_due(now) is due


def test_half_the_lifetime_is_floored_for_an_odd_lifetime() -> None:
    timing = AccessTokenTiming(1_000, 1_101)

    assert (timing.is_due(1_049), timing.is_due(1_050)) == (False, True)


def test_a_240_hour_lifetime_is_due_at_exactly_120_hours() -> None:
    timing = AccessTokenTiming(NOW, NOW + 240 * 3600)

    assert (timing.is_due(NOW + 120 * 3600 - 1), timing.is_due(NOW + 120 * 3600)) == (False, True)
