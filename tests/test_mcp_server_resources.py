"""The person front's resources: one port, answered per verified principal."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from mcp_types import TextResourceContents

from agents.mcp_server import McpPersonFront
from agents.mcp_server.asgi import LOGGER_NAME as LOGGER
from agents.member import (
    STANDING_URI,
    MemberStanding,
    PlaceEntry,
    PlaceRef,
    PlaceView,
    PlaceWindow,
    StandingState,
    place_window_uri,
)
from agents.tools import Principal
from tests.support.mcp_server_support import (
    MemberApp,
    a_member,
    a_standing,
    a_view,
    drive,
    member_scope,
    post,
    resolve_member,
    rpc_body,
    sdk_client,
    view_uri,
)

_UNKNOWN_RESOURCE = "Unknown resource"
_FAILED = "the resource failed"


def a_window(text: str = "hello") -> PlaceWindow:
    entry = PlaceEntry(seq=1, kind="line", name="Ada", text=text, own=False)
    return PlaceWindow(generation="g1", starts_at=1, entries=(entry,))


def a_standing_record(place_id: str) -> MemberStanding:
    return MemberStanding(standings=(a_standing(place_id, StandingState.ACTIVE),))


def window_uri(place_id: str) -> str:
    return place_window_uri(PlaceRef(kind="call", id=place_id))


@pytest.fixture
def app() -> MemberApp:
    alice, bob = a_member("alice"), a_member("bob")
    return MemberApp(
        readable={
            alice: {
                STANDING_URI: a_standing_record("p1"),
                window_uri("p1"): a_window("alice line"),
                view_uri("p1"): a_view(),
            },
            bob: {STANDING_URI: a_standing_record("p2"), window_uri("p2"): a_window("bob line")},
        }
    )


@pytest.fixture
def front(app: MemberApp) -> McpPersonFront:
    return McpPersonFront([], resolve_person=resolve_member, resources=app)


async def exchange(front: McpPersonFront, member: Principal, method: str, **params: Any) -> Any:
    """The whole JSON-RPC message the front answered, through real scopes."""
    async with front.running():
        sent = await post(front, member, rpc_body(method, params))
    return json.loads(b"".join(message.get("body", b"") for message in sent))


def answered(front: McpPersonFront, member: Principal, method: str, **params: Any) -> Any:
    return asyncio.run(exchange(front, member, method, **params))


def test_one_uri_read_by_two_members_answers_each_its_own_record_for_the_verified_principal(
    front: McpPersonFront, app: MemberApp
) -> None:
    alice, bob = a_member("alice"), a_member("bob")

    for_alice = answered(front, alice, "resources/read", uri=STANDING_URI)["result"]
    for_bob = answered(front, bob, "resources/read", uri=STANDING_URI)["result"]

    assert (
        MemberStanding.model_validate_json(for_alice["contents"][0]["text"])
        == (app.readable[alice][STANDING_URI])
    )
    assert (
        MemberStanding.model_validate_json(for_bob["contents"][0]["text"])
        == (app.readable[bob][STANDING_URI])
    )
    assert app.asked == [(alice, STANDING_URI), (bob, STANDING_URI)]


@pytest.mark.parametrize(
    "uri",
    [
        pytest.param(window_uri("p2"), id="another member's window"),
        pytest.param("member://nothing", id="an unknown uri"),
        pytest.param(window_uri("p1")[:-2] + "%70%31", id="a non-canonical spelling"),
        pytest.param(window_uri("p1").upper(), id="a case-changed spelling"),
        pytest.param(view_uri("p2"), id="another member's view"),
        pytest.param(view_uri("p1")[:-2] + "%65w", id="a non-canonical view spelling"),
        pytest.param(view_uri("p1") + "/", id="a view with a trailing slash"),
    ],
)
def test_a_guessed_uri_answers_one_fixed_error_that_does_not_echo_the_uri(
    front: McpPersonFront, uri: str
) -> None:
    reply = answered(front, a_member("alice"), "resources/read", uri=uri)

    assert reply["error"]["code"] == -32602
    assert reply["error"]["message"] == _UNKNOWN_RESOURCE
    assert uri not in json.dumps(reply)


@pytest.mark.parametrize(
    ("sub", "expected"),
    [
        ("alice", sorted([STANDING_URI, window_uri("p1"), view_uri("p1")])),
        ("bob", sorted([STANDING_URI, window_uri("p2")])),
        ("nobody", []),
    ],
)
def test_resources_list_holds_exactly_the_principals_uris_sorted(
    front: McpPersonFront, sub: str, expected: list[str]
) -> None:
    result = answered(front, a_member(sub), "resources/list")["result"]

    assert [resource["uri"] for resource in result["resources"]] == expected
    assert all(
        resource["name"] == resource["uri"] and resource["mimeType"] == "application/json"
        for resource in result["resources"]
    )


@pytest.mark.parametrize(
    ("method", "params"),
    [("resources/list", {}), ("resources/read", {"uri": STANDING_URI})],
)
def test_both_answers_carry_a_zero_ttl_and_a_private_cache_scope_on_the_wire(
    front: McpPersonFront, method: str, params: dict[str, Any]
) -> None:
    result = answered(front, a_member("alice"), method, **params)["result"]

    assert (result["ttlMs"], result["cacheScope"]) == (0, "private")


@pytest.mark.parametrize(
    ("uri", "record_type"),
    [
        (STANDING_URI, MemberStanding),
        (window_uri("p1"), PlaceWindow),
        (view_uri("p1"), PlaceView),
    ],
)
def test_a_runner_parses_what_it_reads_into_the_record_the_app_gave(
    front: McpPersonFront, app: MemberApp, uri: str, record_type: Any
) -> None:
    [content] = answered(front, a_member("alice"), "resources/read", uri=uri)["result"]["contents"]
    record = app.readable[a_member("alice")][uri]

    assert content["uri"] == uri
    assert content["mimeType"] == "application/json"
    assert content["text"] == record.model_dump_json()
    assert record_type.model_validate_json(content["text"]) == record


def test_a_views_state_reaches_the_reader_equal_to_what_the_app_gave(
    front: McpPersonFront, app: MemberApp
) -> None:
    alice = a_member("alice")
    [content] = answered(front, alice, "resources/read", uri=view_uri("p1"))["result"]["contents"]

    wire = json.loads(content["text"])

    given_state = app.readable[alice][view_uri("p1")].state  # type: ignore[union-attr]
    assert wire["state"] == given_state
    assert json.dumps(wire["state"]) == json.dumps(given_state)
    assert wire["revision"] == 1


@pytest.mark.parametrize("with_port", [True, False], ids=["with the port", "without the port"])
def test_discovery_advertises_resources_without_subscribe_only_with_the_port(
    app: MemberApp, with_port: bool
) -> None:
    def fixed_member(_scope: dict[str, Any]) -> Principal:
        return a_member("alice")

    front = McpPersonFront([], resolve_person=fixed_member, resources=app if with_port else None)

    async def capabilities() -> dict[str, Any]:
        async with sdk_client(front) as client:
            return client.server_capabilities.model_dump(exclude_none=True)

    advertised = asyncio.run(capabilities())

    assert ("resources" in advertised) is with_port
    if with_port:
        assert not advertised["resources"].get("subscribe")


def test_without_the_port_resources_list_is_an_unknown_method() -> None:
    front = McpPersonFront([], resolve_person=resolve_member)

    reply = answered(front, a_member("alice"), "resources/list")

    assert reply["error"]["code"] == -32601


@pytest.mark.parametrize(
    ("method", "params"),
    [("resources/list", {}), ("resources/read", {"uri": "marker-uri-91c2"})],
)
@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
def test_a_failing_port_answers_the_fixed_text_and_no_marker_reaches_any_log(
    front: McpPersonFront,
    app: MemberApp,
    caplog: pytest.LogCaptureFixture,
    method: str,
    params: dict[str, Any],
    level: int,
) -> None:
    caplog.set_level(level)
    caplog.set_level(level, logger=LOGGER)
    app.failure = RuntimeError("marker-failure-5d1e")

    reply = answered(front, a_member("marker-principal-3b7a"), method, **params)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert (reply["error"]["code"], reply["error"]["message"]) == (-32603, _FAILED)
    assert "RuntimeError" in logged
    assert not any(
        marker in logged
        for marker in ("marker-uri-91c2", "marker-principal-3b7a", "marker-failure-5d1e")
    )


@pytest.mark.parametrize(
    ("method", "params"),
    [("resources/list", {}), ("resources/read", {"uri": STANDING_URI})],
)
def test_a_cancelled_port_is_not_a_failure_and_propagates(
    front: McpPersonFront, app: MemberApp, method: str, params: dict[str, Any]
) -> None:
    app.failure = asyncio.CancelledError()
    driven = exchange(front, a_member("alice"), method, **params)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(driven)


@pytest.mark.parametrize(
    ("method", "params", "unusable", "raised"),
    [
        ("resources/list", {}, ["member://x", 1], "TypeError"),
        ("resources/list", {}, [1], "ValidationError"),
        ("resources/read", {"uri": STANDING_URI}, {"standings": []}, "AttributeError"),
        ("resources/read", {"uri": STANDING_URI}, object(), "AttributeError"),
    ],
    ids=["mixed uris", "a non-text uri", "a mapping record", "an object record"],
)
def test_a_port_returning_an_unusable_value_answers_the_fixed_text_and_logs_the_type(
    front: McpPersonFront,
    app: MemberApp,
    caplog: pytest.LogCaptureFixture,
    method: str,
    params: dict[str, Any],
    unusable: Any,
    raised: str,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    app.unusable = unusable

    reply = answered(front, a_member("alice"), method, **params)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert (reply["error"]["code"], reply["error"]["message"]) == (-32603, _FAILED)
    assert f"{method} raised {raised}" in logged


def test_the_sdk_client_lists_and_reads_what_the_front_answers(app: MemberApp) -> None:
    alice = a_member("alice")
    front = McpPersonFront([], resolve_person=lambda _scope: alice, resources=app)

    async def through_the_client() -> tuple[list[str], str, int, str]:
        async with sdk_client(front) as client:
            listed = await client.list_resources()
            read = await client.read_resource(STANDING_URI)
            [content] = read.contents
            assert isinstance(content, TextResourceContents)
            return (
                [r.uri for r in listed.resources],
                content.text,
                listed.ttl_ms,
                listed.cache_scope,
            )

    uris, text, ttl_ms, cache_scope = asyncio.run(through_the_client())

    assert uris == sorted(app.readable[alice])
    assert MemberStanding.model_validate_json(text) == app.readable[alice][STANDING_URI]
    assert (ttl_ms, cache_scope) == (0, "private")


def test_the_shell_refuses_an_origin_before_resources_dispatch(
    front: McpPersonFront, app: MemberApp
) -> None:
    body = rpc_body("resources/read", {"uri": STANDING_URI})
    scope = member_scope(a_member("alice"), body)
    scope["headers"].append((b"origin", b"https://elsewhere.test"))

    async def driven() -> list[dict[str, Any]]:
        async with front.running():
            return await drive(front, scope, body)

    sent = asyncio.run(driven())

    assert sent[0]["status"] == 403
    assert app.asked == []


def test_a_repeated_member_is_refused_before_resources_dispatch(
    front: McpPersonFront, app: MemberApp
) -> None:
    body = b'{"jsonrpc":"2.0","id":1,"method":"resources/read","method":"resources/list"}'
    scope = member_scope(a_member("alice"), body)

    async def driven() -> list[dict[str, Any]]:
        async with front.running():
            return await drive(front, scope, body)

    sent = asyncio.run(driven())

    assert json.loads(sent[1]["body"])["error"]["code"] == -32700
    assert app.asked == []


def test_an_answer_carrying_the_request_bearer_is_withheld_and_the_log_names_the_method_only(
    front: McpPersonFront, app: MemberApp, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    token = "head.payload.sigOfTheRequest"
    alice = a_member("alice")
    app.readable[alice][window_uri("p1")] = a_window(f"debug: {token}")
    body = rpc_body("resources/read", {"uri": window_uri("p1")})
    scope = member_scope(alice, body)
    scope["headers"].append((b"authorization", f"Bearer {token}".encode()))

    async def driven() -> list[dict[str, Any]]:
        async with front.running():
            return await drive(front, scope, body)

    sent = asyncio.run(driven())

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert sent[0]["status"] == 500
    assert "resources/read" in logged
    assert not any(word in logged for word in ("sigOfTheRequest", "p1", "member://"))
