"""The person front's resources: one port, answered per verified principal."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from agent_runtime_client.mcp_server import McpPersonFront
from agent_runtime_client.mcp_server.asgi import LOGGER_NAME as LOGGER
from agent_runtime_client.member import (
    STANDING_URI,
    MemberStanding,
    PlaceEntry,
    PlaceRef,
    PlaceWindow,
    place_window_uri,
)
from agent_runtime_client.tools import Principal
from agent_runtime_client.wait import StandingState
from tests.support.mcp_server_support import (
    MemberApp,
    a_member,
    a_standing,
    drive,
    member_scope,
    post,
    resolve_member,
    rpc_body,
    sdk_client,
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
        ("alice", sorted([STANDING_URI, window_uri("p1")])),
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
    [(STANDING_URI, MemberStanding), (window_uri("p1"), PlaceWindow)],
)
def test_a_runner_parses_what_it_reads_into_the_record_the_app_gave(
    front: McpPersonFront, app: MemberApp, uri: str, record_type: Any
) -> None:
    [content] = answered(front, a_member("alice"), "resources/read", uri=uri)["result"]["contents"]

    assert content["uri"] == uri
    assert content["mimeType"] == "application/json"
    assert record_type.model_validate_json(content["text"]) == app.readable[a_member("alice")][uri]


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


def test_without_the_port_resources_list_is_an_unknown_method(app: MemberApp) -> None:
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


def test_a_cancelled_port_is_not_a_failure_and_propagates(app: MemberApp) -> None:
    front = McpPersonFront([], resolve_person=resolve_member, resources=app)
    app.failure = asyncio.CancelledError()

    async def driven() -> None:
        async with front.running():
            await post(front, a_member("alice"), rpc_body("resources/list"))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(driven())


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
