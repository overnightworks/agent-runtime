"""The generator: the routes a host marked with ``agent_tool`` become person-front tools.

The host passes its own ``app.openapi()``, its whole ASGI application and ``mark_route``, which
maps an upper-case method and the document's template to the host's mark type. Every breach of
the supported profile is refused at start with one ``RouteProfileError``.
"""

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from agent_runtime_client.mcp_server.asgi import AsgiApp
from agent_runtime_client.mcp_server.person import PersonRequest, PersonTool, PersonToolHandler
from agent_runtime_client.mcp_server.route_call import RouteTarget, call_route, placeholder_names
from agent_runtime_client.tools import (
    AGENT_TOOL_EXTENSION,
    JsonObject,
    ToolDeclaration,
    ToolEffect,
    ToolOutcome,
)

_SUPPORTED_METHODS: Final = frozenset({"get", "post"})
_OPERATION_METHODS: Final = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_SCALAR_TYPES: Final = frozenset({"string", "integer", "number", "boolean"})
_JSON: Final = "application/json"
_SCHEMA_REFERENCE: Final = "#/components/schemas/"
_ANNOTATIONS: Final = frozenset({"title", "description", "default", "examples", "deprecated"})
_BODY_KEYWORDS: Final = _ANNOTATIONS | {"type", "properties", "required", "additionalProperties"}


class RouteProfileError(ValueError):
    """A marked operation, or its template, is outside what a tool can be made from."""


class _Breach(Exception):  # noqa: N818 - internal; carries the reason an operation is refused
    pass


@dataclass(frozen=True)
class RouteTools[Mark: Hashable]:
    """The tools of the marked routes, and the host's own marks for the same routes."""

    tools: tuple[PersonTool, ...]
    marks: frozenset[Mark]


@dataclass(frozen=True)
class _Operation:
    method: str
    template: str
    node: JsonObject

    @property
    def label(self) -> str:
        return f"{self.node.get('operationId', '-')} {self.method.upper()} {self.template}"


def _could_match(first: str, second: str) -> bool:
    """Two literals match only when equal; a segment holding a parameter matches any other."""
    return first == second or "{" in first or "{" in second


def _overlap(first: str, second: str) -> bool:
    first_segments, second_segments = first.split("/"), second.split("/")
    return len(first_segments) == len(second_segments) and all(
        _could_match(one, other) for one, other in zip(first_segments, second_segments, strict=True)
    )


def _inlined(node: Any, components: JsonObject, seen: tuple[str, ...] = ()) -> Any:
    """The schema with every ``$ref`` replaced by its component.

    An annotation beside a reference wins over the component's own; a recursive reference, or a
    keyword beside it that is no annotation, is a breach.
    """
    if isinstance(node, list):
        return [_inlined(item, components, seen) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" not in node:
        return {key: _inlined(value, components, seen) for key, value in node.items()}
    reference = node["$ref"]
    if not isinstance(reference, str):
        raise _Breach("a $ref must be a string")
    name = reference.removeprefix(_SCHEMA_REFERENCE)
    if name in seen or name not in components:
        raise _Breach("a schema is recursive or unresolved")
    resolved = _inlined(components[name], components, (*seen, name))
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    if siblings.keys() - _ANNOTATIONS:
        raise _Breach("a reference may carry only an annotation beside it")
    return {**resolved, **siblings}


def _scalar(schema: JsonObject) -> bool:
    options = schema.get("anyOf")
    if options is None:
        return schema.get("type") in _SCALAR_TYPES
    present = [option for option in options if option.get("type") != "null"]
    return len(options) == 2 and len(present) == 1 and _scalar(present[0])


def _parameter_schema(parameter: JsonObject, components: JsonObject) -> JsonObject:
    schema = _inlined(parameter.get("schema", {}), components)
    where = parameter["in"]
    if where == "path" and schema.get("type") != "string":
        raise _Breach("a path parameter must be a string")
    if where not in {"path", "query"} or not _scalar(schema):
        raise _Breach(f"the {where} parameter {parameter['name']} is not supported")
    return schema


def _body(operation: _Operation, components: JsonObject) -> tuple[JsonObject, Sequence[str]] | None:
    """The body's properties and required names, or ``None`` for an operation without one."""
    body = operation.node.get("requestBody")
    if body is None:
        return None
    if body.get("required") is not True or set(body.get("content", {})) != {_JSON}:
        raise _Breach("a body must be a required application/json object")
    schema = _inlined(body["content"][_JSON].get("schema", {}), components)
    if schema.get("type") != "object" or "properties" not in schema:
        raise _Breach("a body must be an object")
    if (schema.keys() - _BODY_KEYWORDS) or schema.get("additionalProperties", False) is not False:
        raise _Breach("a body object may carry only its properties, required names and annotations")
    return schema["properties"], schema.get("required", [])


def _check_responses(operation: _Operation, components: JsonObject) -> None:
    successes = 0
    for status, response in operation.node.get("responses", {}).items():
        content = response.get("content", {})
        if not set(content) <= {_JSON}:
            raise _Breach("a response must be JSON")
        if _JSON in content:
            _inlined(content[_JSON].get("schema", {}), components)
        if str(status).startswith("2"):
            if _JSON not in content:
                raise _Breach("a success response must be JSON")
            successes += 1
    if not successes:
        raise _Breach("a success response must be JSON")


def _check_description(operation: _Operation) -> str:
    description = operation.node.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _Breach("a description is required")
    return description


@dataclass
class _Inputs:
    """The input schema being collected; a name given twice is a breach."""

    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)

    def add(self, name: str, schema: Any, *, is_required: bool) -> None:
        if name in self.properties:
            raise _Breach(f"the name {name} is shared by two inputs")
        self.properties[name] = schema
        if is_required:
            self.required.append(name)

    @property
    def schema(self) -> JsonObject:
        return {
            "type": "object",
            "properties": self.properties,
            "required": self.required,
            "additionalProperties": False,
        }


def _declared_inputs(
    operation: _Operation, components: JsonObject
) -> tuple[RouteTarget, JsonObject]:
    """Where each argument goes, and the input schema."""
    inputs = _Inputs()
    paths, queries = [], []
    for parameter in operation.node.get("parameters", []):
        schema = _parameter_schema(parameter, components)
        (paths if parameter["in"] == "path" else queries).append(parameter["name"])
        inputs.add(parameter["name"], schema, is_required=parameter.get("required", False))
    if frozenset(paths) != placeholder_names(operation.template):
        raise _Breach("the path parameters do not match the template")
    body = _body(operation, components)
    for name, schema in (body[0] if body else {}).items():
        inputs.add(name, schema, is_required=name in body[1] if body else False)
    body_names = None if body is None else frozenset(body[0])
    target = RouteTarget(
        operation.method.upper(), operation.template, frozenset(paths), tuple(queries), body_names
    )
    return target, inputs.schema


def _handler(app: AsgiApp, target: RouteTarget) -> PersonToolHandler:
    async def handler(request: PersonRequest, arguments: JsonObject) -> ToolOutcome:
        return await call_route(app, request.scope, target, arguments)

    return handler


def _mark(operation: _Operation) -> tuple[str, ToolEffect]:
    mark = operation.node[AGENT_TOOL_EXTENSION]
    effects = {effect.value: effect for effect in ToolEffect}
    if not (
        isinstance(mark, dict)
        and isinstance(mark.get("name"), str)
        and isinstance(mark.get("effect"), str)
        and mark["effect"] in effects
    ):
        raise _Breach("a mark must be the mapping agent_tool returns")
    return mark["name"], effects[mark["effect"]]


def _tool(operation: _Operation, app: AsgiApp, components: JsonObject) -> PersonTool:
    if operation.method not in _SUPPORTED_METHODS:
        raise _Breach("only GET and POST can be marked")
    _check_responses(operation, components)
    description = _check_description(operation)
    target, parameters = _declared_inputs(operation, components)
    name, effect = _mark(operation)
    declaration = ToolDeclaration(
        name=name, description=description, parameters=parameters, effect=effect
    )
    return PersonTool(declaration, _handler(app, target))


def _operations(openapi: JsonObject) -> list[_Operation]:
    return [
        _Operation(method, template, node)
        for template, item in openapi.get("paths", {}).items()
        for method, node in item.items()
        if method in _OPERATION_METHODS
    ]


def _unshadowed(operation: _Operation, operations: Sequence[_Operation]) -> None:
    for other in operations:
        if other is not operation and other.method == operation.method:
            if _overlap(operation.template, other.template):
                raise _Breach("the template overlaps another operation of its method")


def _marked(
    operation: _Operation,
    operations: Sequence[_Operation],
    app: AsgiApp,
    components: JsonObject,
) -> PersonTool:
    try:
        _unshadowed(operation, operations)
        return _tool(operation, app, components)
    except (_Breach, KeyError, ValueError) as breach:
        raise RouteProfileError(f"{operation.label}: {breach}") from breach


def route_tools[Mark: Hashable](
    openapi: JsonObject, app: AsgiApp, *, mark_route: Callable[[str, str], Mark]
) -> RouteTools[Mark]:
    """The tools and marks of every route marked with ``agent_tool``.

    ``app`` is the host's whole application. ``mark_route`` runs only on marked operations; a
    ``ValueError`` it raises becomes a ``RouteProfileError`` with the host's error as its cause.
    """
    components = openapi.get("components", {}).get("schemas", {})
    operations = _operations(openapi)
    tools: dict[str, PersonTool] = {}
    marks = set()
    for operation in operations:
        if AGENT_TOOL_EXTENSION not in operation.node:
            continue
        tool = _marked(operation, operations, app, components)
        name = tool.declaration.name
        if name in tools:
            raise RouteProfileError(f"{operation.label}: the tool name {name} is marked twice")
        try:
            marks.add(mark_route(operation.method.upper(), operation.template))
        except ValueError as refusal:
            raise RouteProfileError(f"{operation.label}: {refusal}") from refusal
        tools[name] = tool
    return RouteTools(tuple(tools.values()), frozenset(marks))
