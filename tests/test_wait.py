"""The wait answer's records: a standing carries its invitation exactly when it is invited."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from agent_runtime_client.wait import (
    Invitation,
    PlaceRef,
    Standing,
    StandingState,
    WaitAnswer,
)

PLACE = PlaceRef(kind="call", id="c-1")
INVITATION = Invitation(id="i-1", epoch=3, expires_at=datetime(2026, 11, 8, 12, tzinfo=UTC))


@pytest.mark.parametrize(
    ("state", "invitation"),
    [
        (StandingState.INVITED, None),
        (StandingState.ACTIVE, INVITATION),
        (StandingState.PAUSED, INVITATION),
    ],
)
def test_a_standing_carrying_an_invitation_other_than_exactly_when_invited_is_refused(
    state: StandingState, invitation: Invitation | None
) -> None:
    with pytest.raises(ValidationError):
        Standing(place=PLACE, state=state, invitation=invitation)


@pytest.mark.parametrize(
    "fields",
    [
        {"epoch": -1},
        {"epoch": 1.0},
        {"expires_at": datetime(2026, 11, 8, 12)},
        {"id": ""},
    ],
    ids=["negative-epoch", "fractional-epoch", "naive-expiry", "empty-id"],
)
def test_an_invitation_with_a_malformed_field_is_refused(fields: dict[str, Any]) -> None:
    malformed = INVITATION.model_dump() | fields

    with pytest.raises(ValidationError):
        Invitation.model_validate(malformed)


def test_only_an_invited_standing_carries_its_invitations_id_epoch_and_expiry_on_the_wire() -> None:
    answer = WaitAnswer(
        cursor="c",
        standings=(
            Standing(place=PLACE, state=StandingState.INVITED, invitation=INVITATION),
            Standing(place=PlaceRef(kind="call", id="c-2"), state=StandingState.ACTIVE),
            Standing(place=PlaceRef(kind="call", id="c-3"), state=StandingState.PAUSED),
        ),
    )

    invited, active, paused = json.loads(answer.model_dump_json())["standings"]

    assert invited["invitation"] == {"id": "i-1", "epoch": 3, "expires_at": "2026-11-08T12:00:00Z"}
    assert (invited["state"], active["state"], paused["state"]) == ("invited", "active", "paused")
    assert (active["invitation"], paused["invitation"]) == (None, None)


def test_a_wait_answer_reads_back_from_its_json_unchanged() -> None:
    answer = WaitAnswer(
        cursor="c",
        standings=(Standing(place=PLACE, state=StandingState.INVITED, invitation=INVITATION),),
    )

    assert WaitAnswer.model_validate_json(answer.model_dump_json()) == answer


def test_a_standing_in_a_state_the_protocol_does_not_name_is_refused_on_the_wire() -> None:
    wire = {"cursor": "c", "standings": [{"place": {"kind": "call", "id": "c-1"}, "state": "gone"}]}

    payload = json.dumps(wire)

    with pytest.raises(ValidationError):
        WaitAnswer.model_validate_json(payload)
