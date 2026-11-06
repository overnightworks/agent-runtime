"""The gateway backend's logging of a refused call."""

from __future__ import annotations

import json
import logging

import pytest

from agent_runtime_client.backend.fake_gateway import FakeGateway, Reply
from agent_runtime_client.backend.gateway import GatewayBackend
from agent_runtime_client.turn import MAX_EVENT_BYTES
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import UNAVAILABLE, ending

scenario = gateway_turn_support.scenario


def _error_body(**fields: str) -> bytes:
    return json.dumps({"error": {"message": "hash 7f3a and key ending ab12", **fields}}).encode()


@pytest.mark.parametrize(
    ("body", "logged"),
    [
        (_error_body(type="token_not_found_in_db", code="401"), "token_not_found_in_db"),
        (
            _error_body(type="authentication_error", code="401", param="ab12"),
            "authentication_error",
        ),
    ],
    ids=["proxy-key-refusal", "provider-refusal"],
)
def test_a_refusal_logs_the_error_type_and_code_and_never_the_message_or_param(
    scenario, caplog, body, logged
):
    caplog.set_level(logging.INFO)

    _, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(401, body=body)))
    )

    assert (ending(stopped), stopped.detail) == (UNAVAILABLE, "gateway_refused")
    assert logged in caplog.text
    assert "401" in caplog.text
    assert "ab12" not in caplog.text
    assert "7f3a" not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        _error_body(code="401"),
        _error_body(type="authentication_error"),
        _error_body(type="authentication_error", code="4010"),
        _error_body(type="Authentication-Error", code="401"),
        _error_body(type="a" * 65, code="401"),
        b"<html>unauthorized</html>",
        b'{"error":"token_not_found_in_db"}',
        _error_body(type="authentication_error", code="401") + b" " * MAX_EVENT_BYTES,
    ],
    ids=[
        "no-type",
        "no-code",
        "code-of-four-digits",
        "type-outside-the-pattern",
        "type-over-64-characters",
        "not-json",
        "error-is-no-object",
        "over-the-read-bound",
    ],
)
def test_a_refusal_without_a_valid_error_type_and_code_logs_that_no_type_was_given(
    scenario, caplog, body
):
    caplog.set_level(logging.INFO)

    _, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(401, body=body)))
    )

    assert (ending(stopped), stopped.detail) == (UNAVAILABLE, "gateway_refused")
    assert "no error type given" in caplog.text
    assert "authentication_error" not in caplog.text
    assert "ab12" not in caplog.text
