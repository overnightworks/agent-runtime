"""The generator: a marked route of a host's FastAPI app becomes a person-front tool."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

import pytest
from fastapi import Cookie, FastAPI, Header, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_runtime_client.mcp_server import (
    McpPersonFront,
    RouteProfileError,
    route_tools,
)
from agent_runtime_client.mcp_wire import LATEST_PROTOCOL_VERSION, MCP_ENDPOINT_PATH
from agent_runtime_client.tools import Principal, ToolEffect, agent_tool

type Mark = tuple[str, str]


class Genre(StrEnum):
    POP = "pop"
    ROCK = "rock"


class SongDraft(BaseModel):
    title: str
    genre: Genre
    tags: list[str] = []


class Tree(BaseModel):
    children: list["Tree"] = []


class Extra(BaseModel):
    q: str


async def nothing() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class Registration:
    method: str
    path: str
    endpoint: Callable[..., Any] = nothing
    tool: str | None = None
    effect: ToolEffect = ToolEffect.READ_ONLY
    description: str | None = "Does the thing."
    extra: dict[str, Any] = field(default_factory=dict)


def application(*registrations: Registration) -> FastAPI:
    app = FastAPI()
    for registration in registrations:
        mark = (
            agent_tool(name=registration.tool, effect=registration.effect)
            if registration.tool
            else None
        )
        app.add_api_route(
            registration.path,
            registration.endpoint,
            methods=[registration.method],
            openapi_extra=mark,
            description=registration.description,
            **registration.extra,
        )
    return app


def generated(app: FastAPI) -> Any:
    return route_tools(app.openapi(), app, mark_route=lambda method, template: (method, template))


def refused(app: FastAPI) -> RouteProfileError:
    with pytest.raises(RouteProfileError) as raised:
        generated(app)
    return raised.value


async def named(name: str) -> dict[str, str]:
    return {}


async def list_songs(genre: Genre, limit: int = 10, kind: Literal["a", "b"] | None = None) -> Any:
    return {}


async def create_song(draft: SongDraft) -> Any:
    return {"saved": draft.title}


def test_a_marked_route_becomes_a_tool_with_its_name_description_and_input_schema() -> None:
    app = application(Registration("GET", "/songs", list_songs, tool="list_songs"))

    result = generated(app)

    (tool,) = result.tools
    declaration = tool.declaration
    assert (declaration.name, declaration.description) == ("list_songs", "Does the thing.")
    assert declaration.effect is ToolEffect.READ_ONLY
    properties = declaration.parameters["properties"]
    assert properties["genre"]["enum"] == ["pop", "rock"]
    assert properties["kind"]["anyOf"][0]["enum"] == ["a", "b"]
    assert declaration.parameters["required"] == ["genre"]
    assert result.marks == {("GET", "/songs")}


def test_a_body_is_inlined_and_flattened_beside_the_parameters() -> None:
    app = application(
        Registration(
            "POST", "/songs", create_song, tool="create_song", effect=ToolEffect.SIDE_EFFECTING
        )
    )

    (tool,) = generated(app).tools

    parameters = tool.declaration.parameters
    assert set(parameters["properties"]) == {"title", "genre", "tags"}
    assert parameters["properties"]["genre"]["enum"] == ["pop", "rock"]
    assert parameters["required"] == ["title", "genre"]
    assert tool.declaration.effect is ToolEffect.SIDE_EFFECTING


def test_an_unmarked_route_is_no_tool_and_no_mark() -> None:
    app = application(
        Registration("GET", "/songs", list_songs, tool="list_songs"),
        Registration("POST", "/songs/purge", nothing),
    )

    result = generated(app)

    assert [tool.declaration.name for tool in result.tools] == ["list_songs"]
    assert result.marks == {("GET", "/songs")}


def front_for(app: FastAPI) -> TestClient:
    front = McpPersonFront(
        generated(app).tools, resolve_person=lambda scope: Principal(iss="issuer", sub="person")
    )
    return TestClient(Starlette(routes=[Route(MCP_ENDPOINT_PATH, front, methods=["POST"])]))


def rpc(client: TestClient, method: str, params: dict[str, Any] | None = None) -> Any:
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    headers = {
        "MCP-Protocol-Version": str(LATEST_PROTOCOL_VERSION),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    return client.post(MCP_ENDPOINT_PATH, content=json.dumps(message), headers=headers).json()


def test_a_read_only_mark_is_listed_with_the_read_only_hint_and_a_side_effecting_one_without() -> (
    None
):
    app = application(
        Registration("GET", "/songs", list_songs, tool="list_songs"),
        Registration(
            "POST", "/songs", create_song, tool="create_song", effect=ToolEffect.SIDE_EFFECTING
        ),
    )

    listed = rpc(front_for(app), "tools/list")["result"]["tools"]

    hints = {tool["name"]: tool.get("annotations") for tool in listed}
    assert hints == {"list_songs": {"readOnlyHint": True}, "create_song": None}


def test_an_unmarked_route_name_answers_the_unknown_tool_error() -> None:
    app = application(
        Registration("GET", "/songs", list_songs, tool="list_songs"),
        Registration("POST", "/songs/{song_id}/delete", nothing, description="Deletes."),
    )

    answer = rpc(front_for(app), "tools/call", {"name": "delete_song", "arguments": {}})

    assert "error" in answer


def test_a_marked_post_reaches_its_handler_through_the_front() -> None:
    app = application(
        Registration(
            "POST", "/songs", create_song, tool="create_song", effect=ToolEffect.SIDE_EFFECTING
        )
    )
    arguments = {"title": "Dawn", "genre": "pop"}

    answer = rpc(front_for(app), "tools/call", {"name": "create_song", "arguments": arguments})

    assert answer["result"]["isError"] is False
    assert json.loads(answer["result"]["content"][0]["text"]) == {"saved": "Dawn"}


async def header_route(token: Annotated[str, Header()]) -> Any:
    return {}


async def cookie_route(token: Annotated[str, Cookie()]) -> Any:
    return {}


async def optional_body_route(draft: SongDraft | None = None) -> Any:
    return {}


async def recursive_route(tree: Tree) -> Any:
    return {}


async def shared_name_route(q: Annotated[str, Query()], extra: Extra) -> Any:
    return {}


async def integer_path_route(song_id: int) -> Any:
    return {}


@pytest.mark.parametrize(
    "registration",
    [
        Registration("PUT", "/songs", create_song, tool="a"),
        Registration("PATCH", "/songs", create_song, tool="a"),
        Registration("DELETE", "/songs", nothing, tool="a"),
        Registration("GET", "/songs", header_route, tool="a"),
        Registration("GET", "/songs", cookie_route, tool="a"),
        Registration("POST", "/songs", optional_body_route, tool="a"),
        Registration(
            "GET", "/songs", nothing, tool="a", extra={"response_class": PlainTextResponse}
        ),
        Registration("POST", "/songs", recursive_route, tool="a"),
        Registration("POST", "/songs", shared_name_route, tool="a"),
        Registration("GET", "/songs/{song_id}", integer_path_route, tool="a"),
        Registration("GET", "/songs", nothing, tool="a", description=None),
    ],
    ids=[
        "put",
        "patch",
        "delete",
        "header parameter",
        "cookie parameter",
        "optional body",
        "non-JSON response",
        "recursive schema",
        "name shared by a parameter and a body field",
        "integer path parameter",
        "missing description",
    ],
)
def test_a_marked_route_outside_the_profile_is_refused_naming_the_operation(
    registration: Registration,
) -> None:
    failure = refused(application(registration))

    assert registration.method in str(failure)
    assert registration.path in str(failure)


def test_two_marks_of_one_name_are_refused() -> None:
    app = application(
        Registration("GET", "/songs", nothing, tool="same"),
        Registration("GET", "/albums", nothing, tool="same"),
    )

    assert "same" in str(refused(app))


@pytest.mark.parametrize("marked_first", [True, False], ids=["marked first", "marked last"])
@pytest.mark.parametrize(
    ("marked", "other"),
    [
        ("/files/report.json", "/files/{name}.json"),
        ("/files/{name}.json", "/files/report.json"),
        ("/files/{name}", "/files/report.json"),
        ("/files/report.json", "/files/{name}"),
        ("/files/{name}", "/files/{other}"),
    ],
)
def test_a_marked_template_that_overlaps_another_operation_of_its_method_is_refused_in_either_order(
    marked: str, other: str, marked_first: bool
) -> None:
    marked_route = Registration("GET", marked, named if "{name}" in marked else nothing, tool="a")
    other_route = Registration("GET", other, nothing)
    order = (marked_route, other_route) if marked_first else (other_route, marked_route)

    failure = refused(application(*order))

    assert marked in str(failure)


@pytest.mark.parametrize(
    ("marked", "other_method", "other"),
    [
        ("/files/report.json", "GET", "/files/report.csv"),
        ("/files/{name}", "GET", "/files/{name}/versions"),
        ("/files/{name}", "POST", "/files/{name}"),
        ("/files/report.json", "GET", "/documents/{name}.json"),
    ],
)
def test_a_marked_template_that_cannot_collide_with_another_operation_is_accepted(
    marked: str, other_method: str, other: str
) -> None:
    app = application(
        Registration("GET", marked, named, tool="a"), Registration(other_method, other, nothing)
    )

    assert generated(app).marks == {("GET", marked)}


def test_an_unmarked_route_is_never_passed_to_the_host_mark_function() -> None:
    app = application(
        Registration("GET", "/songs", nothing, tool="a"),
        Registration("GET", "/odd/{rest}", nothing),
    )
    seen: list[str] = []

    def mark_route(method: str, template: str) -> Mark:
        seen.append(template)
        return (method, template)

    route_tools(app.openapi(), app, mark_route=mark_route)

    assert seen == ["/songs"]


def test_the_host_mark_functions_value_error_becomes_a_profile_error_with_it_as_cause() -> None:
    app = application(Registration("GET", "/songs", nothing, tool="a"))
    hosts_error = ValueError("template outside the grammar")

    def mark_route(method: str, template: str) -> Mark:
        raise hosts_error

    with pytest.raises(RouteProfileError) as raised:
        route_tools(app.openapi(), app, mark_route=mark_route)

    assert raised.value.__cause__ is hosts_error
    assert "GET" in str(raised.value)
    assert "/songs" in str(raised.value)


def test_another_exception_from_the_host_mark_function_propagates_unchanged() -> None:
    app = application(Registration("GET", "/songs", nothing, tool="a"))

    def mark_route(method: str, template: str) -> Mark:
        raise KeyError(template)

    with pytest.raises(KeyError):
        route_tools(app.openapi(), app, mark_route=mark_route)
