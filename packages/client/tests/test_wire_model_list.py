"""The ``GET /models`` list body, owned by ``agent_runtime_client.wire``."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_runtime_client.wire import PROTOCOL_VERSION, ModelList, is_run_event

MODEL_LIST = {
    "v": PROTOCOL_VERSION,
    "object": "list",
    "data": [
        {"id": "anthropic/opus", "object": "model"},
        {"id": "anthropic/sonnet", "object": "model"},
    ],
}


def test_a_model_list_round_trips_through_its_json() -> None:
    models = ModelList.model_validate_json(json.dumps(MODEL_LIST))

    assert json.loads(models.model_dump_json()) == MODEL_LIST
    assert not is_run_event(models)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**MODEL_LIST, "extra": 1}, id="unknown key"),
        *(
            pytest.param(
                {name: value for name, value in MODEL_LIST.items() if name != missing},
                id=f"no {missing}",
            )
            for missing in MODEL_LIST
            if missing != "object"
        ),
        pytest.param({**MODEL_LIST, "v": PROTOCOL_VERSION - 1}, id="previous version"),
        pytest.param({**MODEL_LIST, "v": True}, id="version true"),
        pytest.param({**MODEL_LIST, "object": "model"}, id="wrong list object"),
        pytest.param({**MODEL_LIST, "data": [{"id": "sonnet", "object": "model"}]}, id="bare name"),
        pytest.param(
            {**MODEL_LIST, "data": [{"id": "Anthropic/Sonnet", "object": "model"}]},
            id="uppercase name",
        ),
        pytest.param(
            {**MODEL_LIST, "data": [{"id": "anthropic/sonnet", "object": "list"}]},
            id="wrong entry object",
        ),
        pytest.param(
            {**MODEL_LIST, "data": [{"id": "anthropic/sonnet", "object": "model", "x": 1}]},
            id="unknown entry key",
        ),
        pytest.param({**MODEL_LIST, "data": [{"object": "model"}]}, id="entry without an id"),
    ],
)
def test_a_model_list_off_the_contract_is_refused(body: dict[str, object]) -> None:
    models_json = json.dumps(body)

    with pytest.raises(ValidationError):
        ModelList.model_validate_json(models_json)
