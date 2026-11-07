"""The port's two sides: who a turn is for, and how a turn ends short of its answer."""

from __future__ import annotations

import pytest

from agent_runtime_client.backend import TurnPerson, TurnStopKind, TurnStopped
from agent_runtime_client.turn import Principal


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
