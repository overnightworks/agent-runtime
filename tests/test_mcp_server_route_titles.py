"""The generator leaves no title in a tool's input schema."""

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from agents.mcp_server import route_tools
from agents.tools import ToolEffect, agent_tool


class Stamp(BaseModel):
    label: str = Field(description="What it says.")


class Folder(BaseModel):
    stamp: Stamp
    stamps: list[Stamp] = []
    note_id: str = Field(description="Which note.")
    title: str


async def file_folder(folder: Folder) -> dict[str, str]:
    """Files a folder."""
    return {}


def folder_app() -> FastAPI:
    app = FastAPI()
    app.add_api_route(
        "/folders",
        file_folder,
        methods=["POST"],
        openapi_extra=agent_tool(name="file_folder", effect=ToolEffect.SIDE_EFFECTING),
    )
    return app


def parameters_of(app: FastAPI, spec: dict[str, Any]) -> dict[str, Any]:
    result = route_tools(spec, app, mark_route=lambda method, template: (method, template))
    (tool,) = result.tools
    return tool.declaration.parameters


def titles_left(node: Any) -> list[str]:
    if isinstance(node, list):
        return [found for item in node for found in titles_left(item)]
    if not isinstance(node, dict):
        return []
    own = ["title"] if isinstance(node.get("title"), str) else []
    return own + [found for value in node.values() for found in titles_left(value)]


def test_no_title_is_left_but_descriptions_and_a_title_property_stay() -> None:
    app = folder_app()

    parameters = parameters_of(app, app.openapi())

    assert titles_left(parameters) == []
    assert parameters["properties"]["note_id"]["description"] == "Which note."
    assert parameters["properties"]["stamp"]["properties"]["label"]["description"] == (
        "What it says."
    )
    assert parameters["properties"]["title"]["type"] == "string"


def test_a_title_beside_a_reference_is_accepted_and_dropped() -> None:
    app = folder_app()
    spec = app.openapi()
    body = spec["components"]["schemas"]["Folder"]["properties"]
    body["stamp"] = {**body["stamp"], "title": "Stamp"}

    assert titles_left(parameters_of(app, spec)) == []
