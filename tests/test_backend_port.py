"""The port's two sides: who a turn is for, and how a turn ends short of its answer."""

from __future__ import annotations

import pytest

from agents.backend import TurnPerson, TurnStopKind, TurnStopped
from agents.turn import (
    FailureCode,
    Principal,
    RejectionCode,
    StopOutcome,
    outcome_of_stop,
)
from agents.voice import DecisionFailed


async def _no_groups() -> frozenset[str]:
    return frozenset()


def test_a_turn_person_is_not_constructible_without_a_principal():
    with pytest.raises(TypeError, match="principal"):
        TurnPerson(role="user", groups=_no_groups)  # type: ignore[call-arg]


def test_a_turn_person_is_frozen():
    person = TurnPerson(principal=Principal(iss="i", sub="s"), role="user", groups=_no_groups)

    with pytest.raises(AttributeError):
        person.role = "admin"  # type: ignore[misc]


def test_a_turn_stop_is_not_constructible_without_a_code() -> None:
    with pytest.raises(TypeError, match="code"):
        TurnStopped(TurnStopKind.REFUSED)  # type: ignore[call-arg]


_OUTCOME_OF_CODE = [
    (RejectionCode.NOT_ENABLED, StopOutcome.NOT_ENABLED),
    (RejectionCode.FORBIDDEN, StopOutcome.NOT_ENABLED),
    (RejectionCode.BUSY, StopOutcome.BUSY),
    (RejectionCode.BUDGET_SPENT, StopOutcome.BUDGET_SPENT),
    *((code, StopOutcome.UNAVAILABLE) for code in FailureCode),
]
_ALL_CODES = [*RejectionCode, *FailureCode]


def _kind_of(code: RejectionCode | FailureCode) -> TurnStopKind:
    return TurnStopKind.REFUSED if isinstance(code, RejectionCode) else TurnStopKind.FAILED


@pytest.mark.parametrize(("code", "outcome"), _OUTCOME_OF_CODE)
def test_every_stop_code_has_exactly_its_outcome(
    code: RejectionCode | FailureCode, outcome: StopOutcome
) -> None:
    assert outcome_of_stop(code) is outcome


def test_the_outcome_table_covers_every_stop_code() -> None:
    assert {code for code, _ in _OUTCOME_OF_CODE} == {*RejectionCode, *FailureCode}


def test_an_outcome_is_never_given_apart_from_its_code() -> None:
    with pytest.raises(TypeError, match="outcome"):
        TurnStopped(  # type: ignore[call-arg]
            TurnStopKind.REFUSED, RejectionCode.BUSY, outcome=StopOutcome.UNAVAILABLE
        )
    with pytest.raises(TypeError):
        DecisionFailed(  # type: ignore[call-arg]
            TurnStopKind.REFUSED, RejectionCode.BUSY, None, StopOutcome.UNAVAILABLE
        )


@pytest.mark.parametrize("code", _ALL_CODES)
def test_a_turn_stop_and_a_failed_decision_of_one_code_give_the_same_outcome(
    code: RejectionCode | FailureCode,
) -> None:
    kind = _kind_of(code)

    assert TurnStopped(kind, code).outcome is outcome_of_stop(code)
    assert DecisionFailed(kind, code, None).outcome is outcome_of_stop(code)


@pytest.mark.parametrize("retry_after_s", [None, 7])
def test_a_busy_refusal_is_busy_with_or_without_a_wait(retry_after_s: int | None) -> None:
    stopped = TurnStopped(TurnStopKind.REFUSED, RejectionCode.BUSY, retry_after_s=retry_after_s)

    assert stopped.outcome is StopOutcome.BUSY
    assert stopped.retry_after_s == retry_after_s
