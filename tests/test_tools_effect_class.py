"""The tool effect class is never shown to a model."""

from __future__ import annotations

import json

import pytest
from packages.client.tests.support.wire_builders import tool_declaration
from tests.support.provider_test_support import SAMPLE_TOOL_MARKUP

from agent_runtime.text_tool_protocol import render_tool_catalog
from agent_runtime.tools import (
    ToolCatalog,
    anthropic_tool_schemas,
    openai_tool_schemas,
)
from agent_runtime_client.tools import ToolEffect


@pytest.mark.parametrize(
    "project", [anthropic_tool_schemas, openai_tool_schemas, render_tool_catalog]
)
def test_the_effect_class_never_reaches_a_model(project) -> None:
    catalog = ToolCatalog(
        tools=(
            tool_declaration("read_note", effect=ToolEffect.READ_ONLY),
            tool_declaration("write_text", effect=ToolEffect.SIDE_EFFECTING),
        ),
        markup=SAMPLE_TOOL_MARKUP,
    )

    seen_by_the_model = json.dumps(project(catalog))

    for leak in ("effect", ToolEffect.READ_ONLY, ToolEffect.SIDE_EFFECTING):
        assert leak not in seen_by_the_model
