import pytest

from agent_runtime_client import capability, turn, wire

MOVED_NAMES = [
    "AgentName",
    "AgentRole",
    "AgentRoles",
    "FailureCode",
    "JsonObject",
    "MAX_AGENT_LENGTH",
    "MAX_EVENT_BYTES",
    "MAX_RUN_FILE_NAME_BYTES",
    "MAX_RUN_FILES_BYTES",
    "MAX_TOOL_INPUT_DEPTH",
    "MODEL_PART_PATTERN",
    "NonEmptyUnicodeText",
    "RejectionCode",
    "RunFile",
    "ToolUseId",
    "Turn",
    "TurnMessage",
    "TurnRole",
    "UnicodeText",
    "base64_length",
    "refuse_non_json_constant",
]


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_wire_re_exports_the_turn_vocabulary_as_the_same_object(name: str) -> None:
    assert getattr(wire, name) is getattr(turn, name)
    assert name in wire.__all__


def test_capability_principal_is_the_turn_principal() -> None:
    assert capability.CapabilityPrincipal is turn.Principal
