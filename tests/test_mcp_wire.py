"""What the person front's wire keeps beside the SDK: pure rules, driven directly."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runtime_client.mcp_wire import (
    MCP_ENDPOINT_PATH,
    carries_signature_of,
    read_strict_json,
)
from agent_runtime_client.turn import MAX_TOOL_INPUT_DEPTH


def nested_in_an_array(depth: int) -> Any:
    value: Any = 0
    for _ in range(depth):
        value = [value]
    return value


def test_the_endpoint_path_is_the_one_the_design_names() -> None:
    assert MCP_ENDPOINT_PATH == "/mcp"


def test_a_document_reads_to_its_value_up_to_the_tool_input_depth() -> None:
    document = {"params": {"p": nested_in_an_array(MAX_TOOL_INPUT_DEPTH)}}

    assert read_strict_json(json.dumps(document).encode()) == document


@pytest.mark.parametrize(
    "body",
    [
        b'{"method": "ping"',
        b'{"x": NaN}',
        b'{"x": Infinity}',
        b'{"x": -Infinity}',
        b'{"x": 1e999}',
        b'{"x": -1e999}',
        b"\xff",
        b"",
        b'{"id": 1, "id": 2}',
        b'{"params": {"note": "n", "note": "m"}}',
        b"[" * 200_000,
    ],
    ids=[
        "truncated",
        "NaN",
        "infinity",
        "negative infinity",
        "overflowing number",
        "negative overflowing number",
        "not utf-8",
        "empty",
        "duplicate member",
        "nested duplicate member",
        "too deep to decode",
    ],
)
def test_a_document_the_strict_reader_refuses_raises_value_error(body: bytes) -> None:
    with pytest.raises(ValueError):
        read_strict_json(body)


@pytest.mark.parametrize(
    ("token", "data", "carried"),
    [
        ("head.payload.sig", b"answer sig tail", True),
        ("head.payload.sig", b"answer head.payload.sig", True),
        ("head.payload.sig", b"answer payload only", False),
        ("sig", b"answer sig", True),
        ("sig", b"answer other", False),
        ("head.payload.sig\u00e9", "answer sig\u00e9".encode(), True),
        ("head.payload.sig\u00e9", b"answer sig", False),
        ("head.payload.sig\u00e9", b"answer sig\\u00e9", True),
        ('head.payload.si"g', b'answer si\\"g', True),
    ],
    ids=[
        "signature",
        "whole token",
        "another token",
        "bare token",
        "bare token absent",
        "non-ascii signature",
        "non-ascii signature absent",
        "non-ascii signature as a json escape",
        "a signature whose quote json escapes",
    ],
)
def test_data_carries_a_token_whole_or_by_its_signature_segment(
    token: str, data: bytes, carried: bool
) -> None:
    assert carries_signature_of(token, data) is carried
