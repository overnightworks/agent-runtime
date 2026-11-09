"""The generator: a marked route of a host's FastAPI app becomes a person-front tool."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

import pytest
from fastapi import Cookie, FastAPI, Header, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.testclient import TestClient

from agent_runtime_client.mcp_server import (
    McpPersonFront,
    RouteProfileError,
    route_tools,
)
from agent_runtime_client.mcp_wire import MCP_ENDPOINT_PATH
from agent_runtime_client.tools import Principal, ToolEffect, agent_tool, not_an_agent_tool
from tests.support.mcp_server_support import routing_headers, rpc_body, served_app

type Mark = tuple[str, str]


class Genre(StrEnum):
    POP = "pop"
    ROCK = "rock"


class NoteDraft(BaseModel):
    title: str
    genre: Genre
    tags: list[str] = []


class Tree(BaseModel):
    children: list["Tree"] = []


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


class Aliased(BaseModel):
    reference: str = Field(alias="$ref")


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
    excluded: str | None = None
    effect: ToolEffect = ToolEffect.READ_ONLY
    description: str | None = "Does the thing."
    extra: dict[str, Any] = field(default_factory=dict)
    document: dict[str, Any] = field(default_factory=dict)


def application(*registrations: Registration) -> FastAPI:
    app = FastAPI()
    for registration in registrations:
        mark = (
            agent_tool(name=registration.tool, effect=registration.effect)
            if registration.tool
            else None
        )
        exclusion = (
            not_an_agent_tool(reason=registration.excluded) if registration.excluded else None
        )
        app.add_api_route(
            registration.path,
            registration.endpoint,
            methods=[registration.method],
            openapi_extra={**(mark or {}), **(exclusion or {}), **registration.document},
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


async def list_notes(genre: Genre, limit: int = 10, kind: Literal["a", "b"] | None = None) -> Any:
    return {}


async def create_note(draft: NoteDraft) -> Any:
    return {"saved": draft.title}


def test_a_marked_route_becomes_a_tool_with_its_name_description_and_input_schema() -> None:
    app = application(Registration("GET", "/notes", list_notes, tool="list_notes"))

    result = generated(app)

    (tool,) = result.tools
    declaration = tool.declaration
    assert (declaration.name, declaration.description) == ("list_notes", "Does the thing.")
    assert declaration.effect is ToolEffect.READ_ONLY
    properties = declaration.parameters["properties"]
    assert properties["genre"]["enum"] == ["pop", "rock"]
    assert properties["kind"]["anyOf"][0]["enum"] == ["a", "b"]
    assert declaration.parameters["required"] == ["genre"]
    assert result.marks == {("GET", "/notes")}


def test_a_body_is_inlined_and_flattened_beside_the_parameters() -> None:
    app = application(
        Registration(
            "POST", "/notes", create_note, tool="create_note", effect=ToolEffect.SIDE_EFFECTING
        )
    )

    (tool,) = generated(app).tools

    parameters = tool.declaration.parameters
    assert set(parameters["properties"]) == {"title", "genre", "tags"}
    assert parameters["properties"]["genre"]["enum"] == ["pop", "rock"]
    assert parameters["required"] == ["title", "genre"]
    assert tool.declaration.effect is ToolEffect.SIDE_EFFECTING


async def described_genre(
    genre: Annotated[Genre, Query(description="Which genre.")] = Genre.ROCK,
) -> Any:
    return {}


def test_annotations_beside_a_reference_are_kept_and_win_over_the_components() -> None:
    app = application(Registration("GET", "/notes", described_genre, tool="a"))
    spec = app.openapi()
    component = spec["components"]["schemas"]["Genre"]
    component.update(description="Any genre.", default="pop")

    (tool,) = route_tools(spec, app, mark_route=lambda method, template: (method, template)).tools

    genre = tool.declaration.parameters["properties"]["genre"]
    assert (genre["description"], genre["default"]) == ("Which genre.", "rock")
    assert genre["enum"] == ["pop", "rock"]


def test_a_constraint_beside_a_reference_is_refused() -> None:
    app = application(Registration("GET", "/notes", list_notes, tool="a"))
    spec = app.openapi()
    (genre,) = [p for p in spec["paths"]["/notes"]["get"]["parameters"] if p["name"] == "genre"]
    genre["schema"] = {**genre["schema"], "maxLength": 5}

    with pytest.raises(RouteProfileError, match="GET /notes"):
        route_tools(spec, app, mark_route=lambda method, template: (method, template))


def test_a_requirement_beside_a_referenced_body_is_refused() -> None:
    app = application(Registration("POST", "/notes", create_note, tool="a"))
    spec = app.openapi()
    content = spec["paths"]["/notes"]["post"]["requestBody"]["content"]["application/json"]
    content["schema"] = {**content["schema"], "required": ["title"]}

    with pytest.raises(RouteProfileError, match="POST /notes"):
        route_tools(spec, app, mark_route=lambda method, template: (method, template))


def edited_body_schema(change: Callable[[dict[str, Any]], None]) -> tuple[FastAPI, Any]:
    app = application(Registration("POST", "/notes", create_note, tool="a"))
    spec = app.openapi()
    change(spec["components"]["schemas"]["NoteDraft"])
    return app, spec


@pytest.mark.parametrize(
    "extra_keywords",
    [
        {"allOf": [{"required": ["title"]}]},
        {"minProperties": 1},
        {"additionalProperties": True},
    ],
    ids=["allOf", "minProperties", "additionalProperties true"],
)
def test_a_body_object_keyword_that_flattening_would_lose_is_refused(
    extra_keywords: dict[str, Any],
) -> None:
    app, spec = edited_body_schema(lambda schema: schema.update(extra_keywords))

    with pytest.raises(RouteProfileError, match="POST /notes"):
        route_tools(spec, app, mark_route=lambda method, template: (method, template))


@pytest.mark.parametrize("model", [NoteDraft, Strict], ids=["plain model", "extra forbid"])
def test_a_plain_body_model_still_becomes_a_tool(model: type[BaseModel]) -> None:
    async def endpoint(draft: model) -> Any:  # type: ignore[valid-type]
        return {}

    result = generated(application(Registration("POST", "/notes", endpoint, tool="a")))

    assert [tool.declaration.name for tool in result.tools] == ["a"]


async def aliased_route(draft: Aliased) -> Any:
    return {}


@pytest.mark.parametrize(
    "mark",
    [
        "a",
        ["a"],
        {"effect": "read_only"},
        {"name": "a"},
        {"name": "a", "effect": "unknown"},
        {"name": "a", "effect": ["read_only"]},
        {"name": 3, "effect": "read_only"},
        {"name": "a", "effect": "read_only", "extra": 1},
    ],
    ids=[
        "string",
        "list",
        "no name",
        "no effect",
        "unknown effect",
        "unhashable effect",
        "name",
        "extra key",
    ],
)
def test_a_hand_written_mark_that_agent_tool_does_not_return_is_refused(mark: Any) -> None:
    app = application(Registration("GET", "/notes", nothing, document={"x-agent-tool": mark}))

    failure = refused(app)

    assert "GET /notes" in str(failure)


def test_an_excluded_route_is_no_tool_and_no_mark() -> None:
    app = application(
        Registration("GET", "/notes", list_notes, tool="list_notes"),
        Registration("POST", "/notes/purge", nothing, excluded="Deletes in bulk."),
    )

    result = generated(app)

    assert [tool.declaration.name for tool in result.tools] == ["list_notes"]
    assert result.marks == {("GET", "/notes")}


def front_for(app: FastAPI) -> TestClient:
    front = McpPersonFront(
        generated(app).tools, resolve_person=lambda scope: Principal(iss="issuer", sub="person")
    )
    return TestClient(served_app(front))


def rpc(client: TestClient, method: str, params: dict[str, Any] | None = None) -> Any:
    body = rpc_body(method, params)
    headers = {name.decode(): value.decode() for name, value in routing_headers(body)}
    with client:
        return client.post(MCP_ENDPOINT_PATH, content=body, headers=headers).json()


def test_a_read_only_mark_is_listed_with_the_read_only_hint_and_a_side_effecting_one_without() -> (
    None
):
    app = application(
        Registration("GET", "/notes", list_notes, tool="list_notes"),
        Registration(
            "POST", "/notes", create_note, tool="create_note", effect=ToolEffect.SIDE_EFFECTING
        ),
    )

    listed = rpc(front_for(app), "tools/list")["result"]["tools"]

    hints = {tool["name"]: tool.get("annotations") for tool in listed}
    assert hints == {"list_notes": {"readOnlyHint": True}, "create_note": None}


def test_an_excluded_route_name_answers_the_unknown_tool_error() -> None:
    app = application(
        Registration("GET", "/notes", list_notes, tool="list_notes"),
        Registration(
            "POST", "/notes/{note_id}/delete", nothing, description="Deletes.", excluded="Deletes."
        ),
    )

    answer = rpc(front_for(app), "tools/call", {"name": "delete_note", "arguments": {}})

    assert "error" in answer


def test_a_marked_post_reaches_its_handler_through_the_front() -> None:
    app = application(
        Registration(
            "POST", "/notes", create_note, tool="create_note", effect=ToolEffect.SIDE_EFFECTING
        )
    )
    arguments = {"title": "Dawn", "genre": "pop"}

    answer = rpc(front_for(app), "tools/call", {"name": "create_note", "arguments": arguments})

    assert answer["result"]["isError"] is False
    assert json.loads(answer["result"]["content"][0]["text"]) == {"saved": "Dawn"}


async def header_route(token: Annotated[str, Header()]) -> Any:
    return {}


async def cookie_route(token: Annotated[str, Cookie()]) -> Any:
    return {}


async def optional_body_route(draft: NoteDraft | None = None) -> Any:
    return {}


async def recursive_route(tree: Tree) -> Any:
    return {}


async def shared_name_route(q: Annotated[str, Query()], extra: Extra) -> Any:
    return {}


async def integer_path_route(note_id: int) -> Any:
    return {}


async def string_path_route(note_id: str) -> Any:
    return {}


async def no_content() -> None:
    return None


def body_of(*media_types: str) -> dict[str, Any]:
    schema = {"type": "object", "properties": {}}
    content = {media_type: {"schema": schema} for media_type in media_types}
    return {"requestBody": {"required": True, "content": content}}


@pytest.mark.parametrize(
    "registration",
    [
        Registration("GET", "/notes", header_route, tool="a"),
        Registration("GET", "/notes", cookie_route, tool="a"),
        Registration("POST", "/notes", optional_body_route, tool="a"),
        Registration(
            "GET", "/notes", nothing, tool="a", extra={"response_class": PlainTextResponse}
        ),
        Registration("POST", "/notes", recursive_route, tool="a"),
        Registration("POST", "/notes", shared_name_route, tool="a"),
        Registration("GET", "/notes/{note_id}", integer_path_route, tool="a"),
        Registration("GET", "/notes", nothing, tool="a", description=None),
        Registration("POST", "/notes", nothing, tool="a", document=body_of("text/plain")),
        Registration(
            "POST", "/notes", nothing, tool="a", document=body_of("application/json", "text/plain")
        ),
        Registration(
            "GET",
            "/notes/{note_id}",
            string_path_route,
            tool="a",
            document={
                "parameters": [{"name": "note_id", "in": "query", "schema": {"type": "string"}}]
            },
        ),
        Registration("GET", "/notes", no_content, tool="a", extra={"status_code": 204}),
        Registration("GET", "/notes", nothing, tool="a", extra={"response_model": Tree}),
        Registration(
            "GET",
            "/notes",
            nothing,
            tool="a",
            document={"responses": {"201": {"description": "Made."}}},
        ),
        Registration(
            "GET",
            "/notes",
            nothing,
            tool="a",
            document={
                "responses": {
                    "200": {
                        "description": "Answer.",
                        "content": {"application/problem+json": {"schema": {"type": "object"}}},
                    }
                }
            },
        ),
        Registration(
            "GET",
            "/notes",
            nothing,
            tool="a",
            document={
                "responses": {
                    "404": {
                        "description": "Answer.",
                        "content": {"text/plain": {"schema": {"type": "object"}}},
                    }
                }
            },
        ),
        Registration("TRACE", "/notes", nothing, tool="a"),
        Registration("HEAD", "/notes", nothing, tool="a"),
        Registration("OPTIONS", "/notes", nothing, tool="a"),
        Registration("POST", "/notes", aliased_route, tool="a"),
    ],
    ids=[
        "header parameter",
        "cookie parameter",
        "optional body",
        "non-JSON response",
        "recursive schema",
        "name shared by a parameter and a body field",
        "integer path parameter",
        "missing description",
        "non-JSON body",
        "body offered in JSON and another type",
        "name shared by a path and a query parameter",
        "response without content",
        "recursive response schema",
        "contentless success beside a JSON success",
        "success declared as problem details",
        "error declared as plain text",
        "trace",
        "head",
        "options",
        "body field named ref",
    ],
)
def test_a_marked_route_outside_the_profile_is_refused_naming_the_operation(
    registration: Registration,
) -> None:
    app = application(registration)
    operation_id = app.openapi()["paths"][registration.path][registration.method.lower()][
        "operationId"
    ]

    failure = refused(app)

    assert registration.method in str(failure)
    assert registration.path in str(failure)
    assert operation_id in str(failure)


@pytest.mark.parametrize(
    "media_types",
    [("application/problem+json",), ("application/json", "application/problem+json")],
    ids=["instead of JSON", "beside JSON"],
)
def test_an_error_status_may_declare_problem_details(media_types: tuple[str, ...]) -> None:
    answer = {media_type: {"schema": {"type": "object"}} for media_type in media_types}
    document = {"responses": {"404": {"description": "Missing.", "content": answer}}}
    app = application(Registration("GET", "/notes", nothing, tool="a", document=document))

    assert generated(app).marks == {("GET", "/notes")}


def test_two_marks_of_one_name_are_refused() -> None:
    app = application(
        Registration("GET", "/notes", nothing, tool="same"),
        Registration("GET", "/folders", nothing, tool="same"),
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
    other_route = Registration("GET", other, nothing, excluded="Not a tool.")
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
        Registration("GET", marked, named, tool="a"),
        Registration(other_method, other, nothing, excluded="Not a tool."),
    )

    assert generated(app).marks == {("GET", marked)}


def test_an_excluded_route_is_never_passed_to_the_host_mark_function() -> None:
    app = application(
        Registration("GET", "/notes", nothing, tool="a"),
        Registration("GET", "/odd/{rest}", nothing, excluded="Odd."),
    )
    seen: list[str] = []

    def mark_route(method: str, template: str) -> Mark:
        seen.append(template)
        return (method, template)

    route_tools(app.openapi(), app, mark_route=mark_route)

    assert seen == ["/notes"]


def test_the_host_mark_functions_value_error_becomes_a_profile_error_with_it_as_cause() -> None:
    app = application(Registration("GET", "/notes", nothing, tool="a"))
    hosts_error = ValueError("template outside the grammar")

    def mark_route(method: str, template: str) -> Mark:
        raise hosts_error

    schema = app.openapi()

    with pytest.raises(RouteProfileError) as raised:
        route_tools(schema, app, mark_route=mark_route)

    assert raised.value.__cause__ is hosts_error
    operation_id = schema["paths"]["/notes"]["get"]["operationId"]
    assert "GET" in str(raised.value)
    assert "/notes" in str(raised.value)
    assert operation_id in str(raised.value)


def test_another_exception_from_the_host_mark_function_propagates_unchanged() -> None:
    app = application(Registration("GET", "/notes", nothing, tool="a"))

    def mark_route(method: str, template: str) -> Mark:
        raise KeyError(template)

    schema = app.openapi()

    with pytest.raises(KeyError):
        route_tools(schema, app, mark_route=mark_route)


def test_an_operation_with_neither_mark_nor_exclusion_is_refused_naming_every_undecided_one() -> (
    None
):
    app = application(
        Registration("GET", "/notes", nothing, tool="a"),
        Registration("POST", "/first", nothing),
        Registration("GET", "/kept", nothing, excluded="Not a tool."),
        Registration("DELETE", "/second/{name}", named),
    )
    schema = app.openapi()
    seen: list[str] = []

    def mark_route(method: str, template: str) -> Mark:
        seen.append(template)
        return (method, template)

    with pytest.raises(RouteProfileError) as raised:
        route_tools(schema, app, mark_route=mark_route)

    message = str(raised.value)
    assert seen == []
    assert "POST /first" in message
    assert "DELETE /second/{name}" in message
    assert message.index("/first") < message.index("/second")
    assert "/kept" not in message
    assert "/notes" not in message


def test_an_undecided_operation_is_refused_even_when_a_marked_one_breaches_the_profile() -> None:
    app = application(
        Registration("GET", "/notes", header_route, tool="a"),
        Registration("GET", "/undecided", nothing),
    )

    failure = str(refused(app))

    assert "/undecided" in failure
    assert "/notes" not in failure


@pytest.mark.parametrize(
    "extension",
    [
        {"excluded": ""},
        {"excluded": "  "},
        {"excluded": 3},
        {"excluded": ["Why."]},
        {"excluded": "Why.", "name": "a", "effect": "read_only"},
        {"excluded": "Why.", "extra": 1},
        {},
    ],
    ids=["empty", "blank", "not a string", "list", "also a mark", "extra key", "empty mapping"],
)
def test_an_exclusion_that_not_an_agent_tool_does_not_return_is_refused(
    extension: Any,
) -> None:
    app = application(Registration("GET", "/notes", nothing, document={"x-agent-tool": extension}))

    assert "GET /notes" in str(refused(app))


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_a_put_patch_or_delete_marked_read_only_is_refused_naming_the_operation(
    method: str,
) -> None:
    app = application(Registration(method, "/notes", nothing, tool="a"))

    failure = str(refused(app))

    assert f"{method} /notes" in failure


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_a_marked_put_patch_or_delete_is_called_with_its_path_query_and_body_arguments(
    method: str,
) -> None:
    async def change(note_id: str, draft: NoteDraft, reason: str = "") -> Any:
        return {"id": note_id, "title": draft.title, "reason": reason}

    app = application(
        Registration(
            method,
            "/notes/{note_id}",
            change,
            tool="change_note",
            effect=ToolEffect.SIDE_EFFECTING,
        )
    )
    arguments = {"note_id": "n1", "title": "Dawn", "genre": "pop", "reason": "typo"}

    answer = rpc(front_for(app), "tools/call", {"name": "change_note", "arguments": arguments})

    assert answer["result"]["isError"] is False
    assert json.loads(answer["result"]["content"][0]["text"]) == {
        "id": "n1",
        "title": "Dawn",
        "reason": "typo",
    }


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE"])
def test_a_head_options_or_trace_route_may_be_excluded_beside_a_marked_tool(method: str) -> None:
    app = application(
        Registration("GET", "/notes", nothing, tool="a"),
        Registration(method, "/probe", nothing, excluded="Not a tool."),
    )

    assert generated(app).marks == {("GET", "/notes")}


@pytest.mark.parametrize(
    "registration",
    [
        Registration("GET", "/stream", header_route, excluded="Needs a header."),
        Registration(
            "POST", "/upload", nothing, excluded="Uploads.", document=body_of("text/plain")
        ),
        Registration(
            "GET",
            "/page",
            nothing,
            excluded="Browser only.",
            extra={"response_class": PlainTextResponse},
        ),
        Registration("GET", "/bare", nothing, excluded="Undescribed.", description=None),
        Registration("DELETE", "/gone", no_content, excluded="Empty.", extra={"status_code": 204}),
        Registration("POST", "/loop", recursive_route, excluded="Recursive."),
    ],
    ids=["header", "non-JSON body", "non-JSON response", "no description", "204", "recursive"],
)
def test_an_excluded_route_outside_the_profile_is_accepted_beside_a_marked_tool(
    registration: Registration,
) -> None:
    app = application(Registration("GET", "/notes", nothing, tool="a"), registration)

    assert generated(app).marks == {("GET", "/notes")}
