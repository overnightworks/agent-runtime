"""A Claude run the CLI ends with an error logs the error's class and none of the CLI's text."""

from __future__ import annotations

import logging

import pytest

from agent_runtime.claude.provider import (
    CLAUDE_CLI_UNAVAILABLE_DETAIL,
    UnavailableError,
    _parse_stream_event,
)

A_CLI_ERROR_TEXT = "the CLI's own words about this error"


@pytest.mark.parametrize(
    ("payload", "logged_class"),
    [
        pytest.param(
            {"type": "assistant", "is_api_error_message": True, "error": "rate_limit"},
            "rate_limit",
            id="assistant-listed-error",
        ),
        pytest.param(
            {"type": "assistant", "is_api_error_message": True, "error": "invalid_request"},
            "invalid_request",
            id="assistant-second-listed-error",
        ),
        pytest.param(
            {"type": "assistant", "is_api_error_message": True, "error": "a_new_class"},
            "other",
            id="assistant-unlisted-error",
        ),
        pytest.param(
            {"type": "assistant", "is_api_error_message": True},
            "other",
            id="assistant-without-error",
        ),
        pytest.param(
            {"type": "result", "is_error": True, "subtype": "error_max_turns"},
            "error_max_turns",
            id="result-listed-subtype",
        ),
        pytest.param(
            {"type": "result", "is_error": True, "subtype": "a_new_subtype"},
            "other",
            id="result-unlisted-subtype",
        ),
        pytest.param(
            {"type": "result", "is_error": True, "subtype": [A_CLI_ERROR_TEXT]},
            "other",
            id="result-subtype-not-a-string",
        ),
        pytest.param({"type": "result", "is_error": True}, "other", id="result-without-subtype"),
    ],
)
def test_a_stream_the_cli_ends_with_an_error_logs_the_errors_class_and_no_cli_text(
    payload: dict, logged_class: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {
        **payload,
        "result": A_CLI_ERROR_TEXT,
        "message": {"content": [{"type": "text", "text": A_CLI_ERROR_TEXT}]},
        "errors": [A_CLI_ERROR_TEXT],
    }

    with pytest.raises(UnavailableError) as ended:
        _parse_stream_event(payload, None)

    assert type(ended.value) is UnavailableError
    assert str(ended.value) == CLAUDE_CLI_UNAVAILABLE_DETAIL
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert logged_class in warning.getMessage()
    assert A_CLI_ERROR_TEXT not in caplog.text
