"""The generator: the routes a host marked with ``agent_tool`` become person-front tools.

The host passes its own ``app.openapi()``, its whole ASGI application and ``mark_route``, which
maps an upper-case method and the document's template to the host's mark type. Every operation
carries a mark or a ``not_an_agent_tool`` exclusion; every breach of the rule or of the supported
profile is refused at start with one ``RouteProfileError``.
"""

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import JsonValue

from agents.mcp_server.asgi import AsgiApp
from agents.mcp_server.person import PersonRequest, PersonTool, PersonToolHandler
from agents.mcp_server.route_call import RouteTarget, call_route, placeholder_names
from agents.tools import (
    AGENT_TOOL_EXTENSION,
    JsonObject,
    ToolDeclaration,
    ToolEffect,
    ToolExclusion,
    ToolMark,
    ToolOutcome,
    read_decision,
)

_SUPPORTED_METHODS: Final = frozenset({"get", "post", "put", "patch", "delete"})
_UNSAFE_METHODS: Final = frozenset({"put", "patch", "delete"})
_OPERATION_METHODS: Final = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
)
_SCALAR_TYPES: Final = frozenset({"string", "integer", "number", "boolean"})
_JSON: Final = "application/json"
_PROBLEM_JSON: Final = "application/problem+json"
_SCHEMA_REFERENCE: Final = "#/components/schemas/"
_ANNOTATIONS: Final = frozenset({"title", "description", "default", "examples", "deprecated"})
_SCHEMA_VALUED: Final = frozenset({"items", "additionalProperties", "not", "contains"})
_SCHEMA_LISTED: Final = frozenset({"anyOf", "allOf", "oneOf", "prefixItems"})
_BODY_KEYWORDS: Final = _ANNOTATIONS | {"type", "properties", "required", "additionalProperties"}


class RouteProfileError(ValueError):
    """An operation is undecided, or a marked one, or its template, is outside what a tool can be.

    An operation is undecided when it carries neither ``agent_tool`` nor ``not_an_agent_tool``.
    """


class _Breach(Exception):  # noqa: N818 - internal; carries the reason an operation is refused
    pass


def _object(node: JsonValue, where: str) -> JsonObject:
    """The node as a JSON object; any other node is a breach naming where it stands."""
    if not isinstance(node, dict):
        raise _Breach(f"{where} must be an object")
    return node


def _array(node: JsonValue, where: str) -> list[JsonValue]:
    if not isinstance(node, list):
        raise _Breach(f"{where} must be an array")
    return node


def _text(node: JsonValue, where: str) -> str:
    if not isinstance(node, str):
        raise _Breach(f"{where} must be a string")
    return node


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


def _inlined(node: JsonValue, components: JsonObject, seen: tuple[str, ...] = ()) -> JsonValue:
    """The schema with every ``$ref`` replaced by its component.

    An annotation beside a reference wins over the component's own; a recursive reference, a
    reference that is no string, a component that is no object, or a keyword beside it that is
    no annotation, is a breach.
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
    resolved = _object(_inlined(components[name], components, (*seen, name)), "a referenced schema")
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    if siblings.keys() - _ANNOTATIONS:
        raise _Breach("a reference may carry only an annotation beside it")
    return {**resolved, **siblings}


def _untitled(node: JsonValue) -> JsonValue:
    """The schema without ``title`` at any depth; a property named ``title`` and data stay."""
    if isinstance(node, list):
        return [_untitled(item) for item in node]
    if not isinstance(node, dict):
        return node
    kept: JsonObject = {}
    for key, value in node.items():
        if key == "title":
            continue
        if key == "properties" and isinstance(value, dict):
            kept[key] = {name: _untitled(schema) for name, schema in value.items()}
        elif key in _SCHEMA_VALUED or key in _SCHEMA_LISTED:
            kept[key] = _untitled(value)
        else:
            kept[key] = value
    return kept


def _schema(node: JsonValue, components: JsonObject, where: str) -> JsonObject:
    """The node as a schema object with every reference inlined and no title; else a breach."""
    return _object(_untitled(_inlined(node, components)), where)


def _scalar(schema: JsonObject) -> bool:
    options = schema.get("anyOf")
    if options is None:
        kind = schema.get("type")
        return isinstance(kind, str) and kind in _SCALAR_TYPES
    branches = [_object(option, "an anyOf option") for option in _array(options, "anyOf")]
    present = [branch for branch in branches if branch.get("type") != "null"]
    return len(branches) == 2 and len(present) == 1 and _scalar(present[0])


def _parameter_schema(parameter: JsonObject, components: JsonObject) -> JsonObject:
    schema = _schema(parameter.get("schema", {}), components, "a parameter schema")
    where = _text(parameter["in"], "a parameter location")
    if where == "path" and schema.get("type") != "string":
        raise _Breach("a path parameter must be a string")
    if where not in {"path", "query"} or not _scalar(schema):
        raise _Breach(f"the {where} parameter {parameter['name']} is not supported")
    return schema


def _body(
    operation: _Operation, components: JsonObject
) -> tuple[JsonObject, frozenset[str]] | None:
    """The body's properties and required names, or ``None`` for an operation without one."""
    requested = operation.node.get("requestBody")
    if requested is None:
        return None
    body = _object(requested, "the request body")
    content = _object(body.get("content", {}), "the request body content")
    if body.get("required") is not True or set(content) != {_JSON}:
        raise _Breach("a body must be a required application/json object")
    media = _object(content[_JSON], "the request body media type")
    schema = _schema(media.get("schema", {}), components, "the request body schema")
    if schema.get("type") != "object" or "properties" not in schema:
        raise _Breach("a body must be an object")
    if (schema.keys() - _BODY_KEYWORDS) or schema.get("additionalProperties", False) is not False:
        raise _Breach("a body object may carry only its properties, required names and annotations")
    properties = _object(schema["properties"], "the body properties")
    names = _array(schema.get("required", []), "the body's required names")
    return properties, frozenset(_text(name, "a required name") for name in names)


def _check_responses(operation: _Operation, components: JsonObject) -> None:
    successes = 0
    for status, node in _object(operation.node.get("responses", {}), "the responses").items():
        response = _object(node, "a response")
        content = _object(response.get("content", {}), "a response content")
        if str(status).startswith("2"):
            if set(content) != {_JSON}:
                raise _Breach("a success response must be JSON")
            successes += 1
        elif not set(content) <= {_JSON, _PROBLEM_JSON}:
            raise _Breach("an error response must be JSON or RFC 9457 problem details")
        for declared in content.values():
            media = _object(declared, "a response media type")
            _schema(media.get("schema", {}), components, "a response schema")
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

    properties: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    required: list[JsonValue] = field(default_factory=list[JsonValue])

    def add(self, name: str, schema: JsonValue, *, is_required: bool) -> None:
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
    paths: list[str] = []
    queries: list[str] = []
    for node in _array(operation.node.get("parameters", []), "the parameters"):
        parameter = _object(node, "a parameter")
        schema = _parameter_schema(parameter, components)
        name = _text(parameter["name"], "a parameter name")
        (paths if parameter["in"] == "path" else queries).append(name)
        inputs.add(name, schema, is_required=bool(parameter.get("required", False)))
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


def _mark(operation: _Operation) -> ToolMark:
    mark = read_decision(operation.node[AGENT_TOOL_EXTENSION])
    if not isinstance(mark, ToolMark):
        raise _Breach("a decision must be the mapping agent_tool or not_an_agent_tool returns")
    return mark


def _tool(operation: _Operation, app: AsgiApp, components: JsonObject) -> PersonTool:
    if operation.method not in _SUPPORTED_METHODS:
        raise _Breach("only GET, POST, PUT, PATCH and DELETE can be marked")
    _check_responses(operation, components)
    description = _check_description(operation)
    target, parameters = _declared_inputs(operation, components)
    mark = _mark(operation)
    name, effect = mark.name, mark.effect
    if operation.method in _UNSAFE_METHODS and effect is ToolEffect.READ_ONLY:
        raise _Breach("a PUT, PATCH or DELETE is not safe and cannot be marked read-only")
    declaration = ToolDeclaration(
        name=name, description=description, parameters=parameters, effect=effect
    )
    return PersonTool(declaration, _handler(app, target))


def _components(openapi: JsonObject) -> JsonObject:
    components = _object(openapi.get("components", {}), "the components")
    return _object(components.get("schemas", {}), "the component schemas")


def _operations(openapi: JsonObject) -> list[_Operation]:
    return [
        _Operation(method, template, _object(node, "an operation"))
        for template, item in _object(openapi.get("paths", {}), "the paths").items()
        for method, node in _object(item, "a path item").items()
        if method in _OPERATION_METHODS
    ]


def _unshadowed(operation: _Operation, operations: Sequence[_Operation]) -> None:
    for other in operations:
        if other is not operation and other.method == operation.method:
            if _overlap(operation.template, other.template):
                raise _Breach("the template overlaps another operation of its method")


def _is_excluded(operation: _Operation) -> bool:
    """Whether the operation carries an exclusion; one without a non-blank reason is a breach."""
    return isinstance(read_decision(operation.node[AGENT_TOOL_EXTENSION]), ToolExclusion)


def _decided(
    operation: _Operation,
    operations: Sequence[_Operation],
    app: AsgiApp,
    components: JsonObject,
) -> PersonTool | None:
    """The tool of a marked operation, or ``None`` for an excluded one, which is not judged."""
    try:
        if _is_excluded(operation):
            return None
        _unshadowed(operation, operations)
        return _tool(operation, app, components)
    except (_Breach, KeyError, ValueError) as breach:
        raise RouteProfileError(f"{operation.label}: {breach}") from breach


def _require_decided(operations: Sequence[_Operation]) -> None:
    undecided = [op.label for op in operations if AGENT_TOOL_EXTENSION not in op.node]
    if undecided:
        raise RouteProfileError(
            "operations with neither agent_tool nor not_an_agent_tool: " + "; ".join(undecided)
        )


def route_tools[Mark: Hashable](
    openapi: JsonObject, app: AsgiApp, *, mark_route: Callable[[str, str], Mark]
) -> RouteTools[Mark]:
    """The tools and marks of every route marked with ``agent_tool``.

    Every operation of the document must carry ``agent_tool`` or ``not_an_agent_tool``. Any
    undecided operation is refused first, all of them named in document order, before a tool is
    built or ``mark_route`` runs. An excluded operation is never judged against the profile and
    never reaches ``mark_route``, but still counts for the overlap check. ``app`` is the host's
    whole application. A ``ValueError`` from ``mark_route`` becomes a ``RouteProfileError`` with
    the host's error as its cause.
    """
    try:
        components = _components(openapi)
        operations = _operations(openapi)
    except _Breach as breach:
        raise RouteProfileError(str(breach)) from breach
    _require_decided(operations)
    tools: dict[str, PersonTool] = {}
    marks: set[Mark] = set()
    for operation in operations:
        tool = _decided(operation, operations, app, components)
        if tool is None:
            continue
        name = tool.declaration.name
        if name in tools:
            raise RouteProfileError(f"{operation.label}: the tool name {name} is marked twice")
        try:
            marks.add(mark_route(operation.method.upper(), operation.template))
        except ValueError as refusal:
            raise RouteProfileError(f"{operation.label}: {refusal}") from refusal
        tools[name] = tool
    return RouteTools(tuple(tools.values()), frozenset(marks))
