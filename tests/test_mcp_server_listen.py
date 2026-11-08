"""The person front's ``subscriptions/listen``: one stream per verified principal.

The virtual clock proves what a hand-driven ASGI connection can show; the two real-clock tests
at the end drive the SDK's own client over a real uvicorn, because only a real socket streams.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from typing import Any

import pytest
from mcp.client.subscriptions import SubscriptionLost

from agent_runtime_client.mcp_server import McpPersonFront, ResourceChanges
from agent_runtime_client.mcp_server.asgi import LOGGER_NAME as LOGGER
from agent_runtime_client.member import (
    LISTEN_BOUND_SECONDS,
    STANDING_URI,
    MemberStanding,
    PlaceRef,
    place_window_uri,
)
from agent_runtime_client.tools import Principal
from tests.support.mcp_server_support import (
    ListenConnection,
    MemberApp,
    a_member,
    http_scope,
    listen_connection,
    loopback_client,
    member_scope,
    open_stream,
    post,
    resolve_member,
    routing_headers,
    rpc_body,
    settled,
)
from tests.support.virtual_time import in_virtual_time

_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"
_FAILED_TEXT = "the resource failed"
_ACKNOWLEDGED = "notifications/subscriptions/acknowledged"


def window_uri(place_id: str) -> str:
    return place_window_uri(PlaceRef(kind="call", id=place_id))


def an_empty_record() -> MemberStanding:
    return MemberStanding(standings=())


def readable_uris(*uris: str) -> dict[str, MemberStanding]:
    return dict.fromkeys(uris, an_empty_record())


@pytest.fixture
def alice() -> Principal:
    return a_member("alice")


@pytest.fixture
def bob() -> Principal:
    return a_member("bob")


@pytest.fixture
def app(alice: Principal, bob: Principal) -> MemberApp:
    return MemberApp(
        readable={
            alice: readable_uris(STANDING_URI, window_uri("first"), window_uri("second")),
            bob: readable_uris(STANDING_URI),
        }
    )


@pytest.fixture
def changes() -> ResourceChanges:
    return ResourceChanges()


@pytest.fixture
def front(app: MemberApp, changes: ResourceChanges) -> McpPersonFront:
    return McpPersonFront([], resolve_person=resolve_member, resources=app, changes=changes)


def served(main: Coroutine[Any, Any, None]) -> None:
    in_virtual_time(main)


def acknowledged_filter(connection: ListenConnection) -> dict[str, Any]:
    first = connection.frames[0]
    assert first["method"] == _ACKNOWLEDGED
    return first["params"]["notifications"]


@pytest.mark.parametrize(
    ("requested", "flags", "honored"),
    [
        pytest.param(
            [STANDING_URI, window_uri("second"), window_uri("first"), STANDING_URI],
            {},
            {"resourceSubscriptions": [STANDING_URI, window_uri("second"), window_uri("first")]},
            id="request order without duplicates",
        ),
        pytest.param(
            [window_uri("foreign"), STANDING_URI],
            {},
            {"resourceSubscriptions": [STANDING_URI]},
            id="only what the principal may read",
        ),
        pytest.param(
            [STANDING_URI],
            {"toolsListChanged": True, "resourcesListChanged": True, "promptsListChanged": True},
            {"resourceSubscriptions": [STANDING_URI]},
            id="no list-changed flag is honored",
        ),
    ],
)
def test_the_first_frame_acknowledges_exactly_the_requested_uris_the_principal_may_read(
    front: McpPersonFront,
    alice: Principal,
    requested: list[str],
    flags: dict[str, bool],
    honored: dict[str, Any],
) -> None:
    connection = listen_connection(front, alice, requested, flags=flags)

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            pass

    served(scenario())

    assert acknowledged_filter(connection) == honored
    assert connection.status == 200
    assert connection.frames[0]["params"]["_meta"][_SUBSCRIPTION_ID] == 1


def test_an_empty_honored_set_is_acknowledged_and_then_silent_until_the_bound(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [window_uri("foreign")])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, STANDING_URI)
            await asyncio.sleep(LISTEN_BOUND_SECONDS * 2)

    served(scenario())

    assert acknowledged_filter(connection) == {}
    assert connection.updated_uris == []
    assert ["result" in frame for frame in connection.frames] == [False, True]


@pytest.mark.parametrize(
    "signal_moment",
    ["while the port answers", "after the acknowledgement was written"],
)
def test_a_change_around_the_acknowledgement_arrives_after_it(
    front: McpPersonFront,
    app: MemberApp,
    alice: Principal,
    changes: ResourceChanges,
    signal_moment: str,
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    def signal_once() -> None:
        app.before_current = lambda: None
        changes.changed(alice, STANDING_URI)

    async def scenario() -> None:
        if signal_moment == "while the port answers":
            app.before_current = signal_once
        async with front.running(), open_stream(connection):
            if signal_moment != "while the port answers":
                signal_once()
            await settled()

    served(scenario())

    assert [frame.get("method") for frame in connection.frames] == [
        _ACKNOWLEDGED,
        "notifications/resources/updated",
    ]
    assert connection.updated_uris == [STANDING_URI]


def test_a_parent_subscription_receives_a_new_place_only_when_the_principal_may_read_it(
    front: McpPersonFront, app: MemberApp, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])
    new_place = window_uri("opened-after-the-acknowledgement")

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            app.readable[alice][new_place] = an_empty_record()
            changes.changed(alice, window_uri("foreign"))
            changes.changed(alice, new_place)
            await settled()

    served(scenario())

    assert connection.updated_uris == [new_place]


def test_an_exact_place_subscriber_gets_that_place_and_no_standing(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [window_uri("first")])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, STANDING_URI)
            changes.changed(alice, window_uri("second"))
            changes.changed(alice, window_uri("first"))
            await settled()

    served(scenario())

    assert connection.updated_uris == [window_uri("first")]


def test_a_pulse_queued_before_a_removal_is_not_delivered(
    front: McpPersonFront, app: MemberApp, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, window_uri("first"))
            del app.readable[alice][window_uri("first")]
            await settled()

    served(scenario())

    assert connection.updated_uris == []


def test_one_principals_streams_each_receive_a_change_and_no_other_member_does(
    front: McpPersonFront,
    app: MemberApp,
    alice: Principal,
    bob: Principal,
    changes: ResourceChanges,
) -> None:
    alice_elsewhere = a_member("alice", "https://other-issuer.test")
    app.readable[alice_elsewhere] = readable_uris(STANDING_URI)
    streams = {
        "alice one": listen_connection(front, alice, [STANDING_URI]),
        "alice two": listen_connection(front, alice, [STANDING_URI]),
        "bob": listen_connection(front, bob, [STANDING_URI]),
        "alice at another issuer": listen_connection(front, alice_elsewhere, [STANDING_URI]),
    }

    async def scenario() -> None:
        async with (
            front.running(),
            open_stream(streams["alice one"]),
            open_stream(streams["alice two"]),
            open_stream(streams["bob"]),
            open_stream(streams["alice at another issuer"]),
        ):
            changes.changed(alice, STANDING_URI)
            await settled()

    served(scenario())

    assert {name: stream.updated_uris for name, stream in streams.items()} == {
        "alice one": [STANDING_URI],
        "alice two": [STANDING_URI],
        "bob": [],
        "alice at another issuer": [],
    }


def test_at_the_bound_the_stream_ends_with_the_completion_as_its_last_frame(
    front: McpPersonFront, alice: Principal
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI], request_id="a-stream-id")

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            await asyncio.sleep(LISTEN_BOUND_SECONDS * 2)

    served(scenario())

    completion = connection.frames[-1]
    assert completion["id"] == "a-stream-id"
    assert completion["result"]["_meta"][_SUBSCRIPTION_ID] == "a-stream-id"
    assert connection.sent[-1]["more_body"] is False
    assert connection.returned_after == LISTEN_BOUND_SECONDS


def test_a_clients_disconnect_frees_its_stream_with_no_completion_frame(
    front: McpPersonFront, app: MemberApp, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            pass
        asked_before = len(app.current_asked)
        changes.changed(alice, STANDING_URI)
        await settled()
        assert len(app.current_asked) == asked_before

    served(scenario())

    assert connection.returned_after is not None
    assert not any("result" in frame for frame in connection.frames)


@pytest.mark.parametrize(
    ("port_answers", "frames"),
    [("for the acknowledgement", []), ("during delivery", [_ACKNOWLEDGED])],
)
def test_a_disconnect_while_the_port_answers_is_no_failure_and_writes_no_completion(
    front: McpPersonFront,
    app: MemberApp,
    alice: Principal,
    changes: ResourceChanges,
    caplog: pytest.LogCaptureFixture,
    port_answers: str,
    frames: list[str],
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        if port_answers == "for the acknowledgement":
            app.current_seconds = LISTEN_BOUND_SECONDS * 2
        async with front.running(), open_stream(connection):
            if port_answers == "during delivery":
                app.current_seconds = LISTEN_BOUND_SECONDS * 2
                changes.changed(alice, STANDING_URI)
                await settled()

    served(scenario())

    assert connection.returned_after is not None
    assert [frame.get("method") for frame in connection.frames] == frames
    assert not any("raised" in record.getMessage() for record in caplog.records)


def test_a_port_that_never_answers_ends_at_the_bound_with_the_completion_alone(
    front: McpPersonFront, app: MemberApp, alice: Principal
) -> None:
    app.current_seconds = LISTEN_BOUND_SECONDS * 2
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            await asyncio.sleep(LISTEN_BOUND_SECONDS * 2)

    served(scenario())

    assert ["result" in frame for frame in connection.frames] == [True]


def test_a_stalled_client_cannot_hold_the_call_past_the_cap(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI], stall_at=0)

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, STANDING_URI)
            await asyncio.sleep(LISTEN_BOUND_SECONDS * 2)

    served(scenario())

    assert connection.returned_after == LISTEN_BOUND_SECONDS + 5
    assert not any("result" in frame for frame in connection.frames)


def test_a_client_that_stalls_past_the_bound_still_gets_the_completion_the_handler_ended_with(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI], stall_at=0)

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, STANDING_URI)
            await asyncio.sleep(LISTEN_BOUND_SECONDS + 1)
            connection.release()
            await settled()

    served(scenario())

    assert [frame.get("method") for frame in connection.frames[:-1]] == [_ACKNOWLEDGED]
    assert "result" in connection.frames[-1]
    assert connection.returned_after is not None


@pytest.mark.parametrize(
    ("bearer", "signature"),
    [("head.payload.sigOfTheStream", "sigOfTheStream"), ("caf\u00e9", "caf\u00e9")],
    ids=["ascii", "non-ascii, which the frame writes as a json escape"],
)
def test_a_bearer_signature_in_the_request_id_answers_an_empty_500_with_no_frame(
    front: McpPersonFront,
    alice: Principal,
    caplog: pytest.LogCaptureFixture,
    bearer: str,
    signature: str,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    connection = listen_connection(
        front, alice, [STANDING_URI], request_id=signature, bearer=bearer
    )

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            pass

    served(scenario())

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert (connection.status, connection.body_bytes) == (500, b"")
    assert "subscriptions/listen" in logged
    assert signature not in logged


def test_a_bearer_signature_in_a_later_frame_cuts_the_stream_before_that_frame(
    front: McpPersonFront,
    app: MemberApp,
    alice: Principal,
    changes: ResourceChanges,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    carrying = window_uri("sigOfTheStream")
    app.readable[alice][carrying] = an_empty_record()
    connection = listen_connection(
        front, alice, [STANDING_URI], bearer="head.payload.sigOfTheStream"
    )

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, carrying)
            await settled()
            assert connection.returned_after is not None

    served(scenario())

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert [frame.get("method") for frame in connection.frames] == [_ACKNOWLEDGED]
    assert b"sigOfTheStream" not in connection.body_bytes
    assert "subscriptions/listen" in logged
    assert "sigOfTheStream" not in logged


def test_a_port_raising_at_the_acknowledgement_answers_the_fixed_text(
    front: McpPersonFront, app: MemberApp, alice: Principal
) -> None:
    app.failure = RuntimeError("marker")
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            pass

    served(scenario())

    reply = json.loads(connection.body_bytes)
    assert (reply["error"]["code"], reply["error"]["message"]) == (-32603, _FAILED_TEXT)


def test_a_port_raising_during_delivery_sends_no_frame_and_completes(
    front: McpPersonFront, app: MemberApp, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            app.failure = RuntimeError("marker")
            changes.changed(alice, STANDING_URI)
            await settled()
            assert connection.returned_after is not None

    served(scenario())

    assert connection.updated_uris == []
    assert "result" in connection.frames[-1]


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
@pytest.mark.parametrize("raises_at", ["acknowledgement", "delivery"])
def test_no_marker_from_a_uri_a_principal_or_a_failure_reaches_any_log(
    front: McpPersonFront,
    app: MemberApp,
    changes: ResourceChanges,
    caplog: pytest.LogCaptureFixture,
    level: int,
    raises_at: str,
) -> None:
    caplog.set_level(level)
    caplog.set_level(level, logger=LOGGER)
    member = a_member("marker-principal-3b7a")
    uri = window_uri("marker-uri-91c2")
    app.readable[member] = readable_uris(STANDING_URI, uri)
    connection = listen_connection(front, member, [STANDING_URI, uri])
    failure = RuntimeError("marker-failure-5d1e")

    async def scenario() -> None:
        if raises_at == "acknowledgement":
            app.failure = failure
        async with front.running(), open_stream(connection):
            app.failure = failure
            changes.changed(member, uri)
            await settled()

    served(scenario())

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert not any(
        marker in logged
        for marker in ("marker-uri-91c2", "marker-principal-3b7a", "marker-failure-5d1e")
    )


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("tools/list", {}),
        ("resources/list", {}),
        ("resources/read", {"uri": STANDING_URI}),
    ],
)
def test_every_other_method_answers_the_same_bytes_as_a_front_without_listen(
    front: McpPersonFront, app: MemberApp, alice: Principal, method: str, params: dict[str, Any]
) -> None:
    without_listen = McpPersonFront([], resolve_person=resolve_member, resources=app)
    body = rpc_body(method, params)

    async def answers(candidate: McpPersonFront) -> list[dict[str, Any]]:
        async with candidate.running():
            return await post(candidate, alice, body)

    assert asyncio.run(answers(front)) == asyncio.run(answers(without_listen))


def _listen_request() -> bytes:
    return rpc_body("subscriptions/listen", {"notifications": {"resourceSubscriptions": []}})


@pytest.mark.parametrize(
    ("body", "headers", "status"),
    [
        pytest.param(_listen_request(), [(b"origin", b"https://x.test")], 403, id="an origin"),
        pytest.param(
            _listen_request().replace(b'"jsonrpc"', b'"id": 2, "jsonrpc"'),
            [],
            400,
            id="a repeated member",
        ),
    ],
)
def test_the_shell_refuses_before_a_listen_opens(
    front: McpPersonFront,
    app: MemberApp,
    alice: Principal,
    body: bytes,
    headers: list[tuple[bytes, bytes]],
    status: int,
) -> None:
    scope = member_scope(alice, body)
    scope["headers"].extend(headers)
    connection = ListenConnection(front, scope, body)

    async def scenario() -> None:
        async with front.running():
            await connection.run()

    served(scenario())

    assert connection.status == status
    assert app.current_asked == []


def test_a_listen_at_another_revision_is_refused_before_it_opens(
    front: McpPersonFront, app: MemberApp, alice: Principal
) -> None:
    body = _listen_request()
    identity = [(b"x-iss", alice.iss.encode()), (b"x-sub", alice.sub.encode())]
    scope = http_scope([*identity, *routing_headers(body, "2025-11-25")])
    connection = ListenConnection(front, scope, body)

    async def scenario() -> None:
        async with front.running():
            await connection.run()

    served(scenario())

    assert connection.status == 400
    assert json.loads(connection.body_bytes)["error"]["data"]["requested"] == "2025-11-25"
    assert app.current_asked == []


def test_a_listen_without_a_streaming_accept_is_not_acceptable(
    front: McpPersonFront, app: MemberApp, alice: Principal
) -> None:
    body = _listen_request()
    scope = member_scope(alice, body)
    scope["headers"] = [
        (name, b"application/json" if name == b"accept" else value)
        for name, value in scope["headers"]
    ]
    connection = ListenConnection(front, scope, body)

    async def scenario() -> None:
        async with front.running():
            await connection.run()

    served(scenario())

    assert connection.status == 406
    assert app.current_asked == []


def test_discovery_advertises_subscribe_and_no_list_changed_with_listen_served(
    front: McpPersonFront, alice: Principal
) -> None:
    async def discovered() -> Any:
        async with front.running():
            sent = await post(front, alice, rpc_body("server/discover", {}))
        return json.loads(b"".join(message.get("body", b"") for message in sent))["result"]

    capabilities = asyncio.run(discovered())["capabilities"]

    assert capabilities["resources"]["subscribe"] is True
    assert not any(capability.get("listChanged") for capability in capabilities.values())


@pytest.mark.parametrize(
    ("notifications", "code"),
    [({"resourceSubscriptions": [STANDING_URI]}, -32601), ("not a filter", -32602)],
    ids=["well-formed", "malformed"],
)
def test_a_front_without_changes_does_not_serve_listen(
    app: MemberApp, alice: Principal, notifications: Any, code: int
) -> None:
    front = McpPersonFront([], resolve_person=resolve_member, resources=app)
    body = rpc_body("subscriptions/listen", {"notifications": notifications})
    connection = ListenConnection(front, member_scope(alice, body), body)

    async def scenario() -> None:
        async with front.running():
            await connection.run()

    served(scenario())

    assert json.loads(connection.body_bytes)["error"]["code"] == code


def test_changes_without_the_resources_port_are_refused_at_construction(
    changes: ResourceChanges,
) -> None:
    with pytest.raises(ValueError, match="resources"):
        McpPersonFront([], resolve_person=resolve_member, changes=changes)


@pytest.mark.parametrize("method", ["tools/list", "subscriptions/listen"])
def test_a_non_ascii_bearer_is_answered_instead_of_raising_out_of_the_front(
    front: McpPersonFront, alice: Principal, method: str
) -> None:
    params = (
        {"notifications": {"resourceSubscriptions": [STANDING_URI]}}
        if method == "subscriptions/listen"
        else {}
    )
    body = rpc_body(method, params)
    scope = member_scope(alice, body)
    scope["headers"].append((b"authorization", b"Bearer caf\xe9"))
    connection = ListenConnection(front, scope, body)

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            pass

    served(scenario())

    assert connection.status == 200


def test_the_sdk_client_is_acknowledged_receives_changes_and_ends_at_the_graceful_completion(
    front: McpPersonFront, app: MemberApp, alice: Principal, changes: ResourceChanges
) -> None:
    foreign = window_uri("foreign")
    new_place = window_uri("opened-after-the-acknowledgement")

    async def through_a_socket() -> tuple[list[str] | None, list[str], list[str] | None]:
        async with loopback_client(front, alice) as client:
            async with client.listen(resource_subscriptions=[STANDING_URI, foreign]) as stream:
                honored = stream.honored.resource_subscriptions
                app.readable[alice][new_place] = an_empty_record()
                changes.changed(alice, foreign)
                changes.changed(alice, new_place)
                changes.changed(alice, STANDING_URI)
                delivered: list[str] = []
                async for event in stream:
                    delivered.append(event.uri)
                    if event.uri == STANDING_URI:
                        app.failure = RuntimeError("the port ends the stream")
                        changes.changed(alice, STANDING_URI)
            app.failure = None
            async with client.listen(resource_subscriptions=[STANDING_URI]) as again:
                honored_again = again.honored.resource_subscriptions
        return honored, delivered, honored_again

    assert asyncio.run(through_a_socket()) == (
        [STANDING_URI],
        [new_place, STANDING_URI],
        [STANDING_URI],
    )


def test_an_abrupt_cut_raises_subscription_lost_at_the_sdk_client_and_a_relisten_is_acknowledged(
    front: McpPersonFront, app: MemberApp, alice: Principal, changes: ResourceChanges
) -> None:
    carrying = window_uri("sigOfTheStream")
    app.readable[alice][carrying] = an_empty_record()

    async def through_a_socket() -> list[str] | None:
        async with loopback_client(front, alice, bearer="head.payload.sigOfTheStream") as client:
            async with client.listen(resource_subscriptions=[STANDING_URI]) as stream:
                changes.changed(alice, carrying)
                with pytest.raises(SubscriptionLost):
                    async for _event in stream:
                        pass
            async with client.listen(resource_subscriptions=[STANDING_URI]) as again:
                return again.honored.resource_subscriptions

    assert asyncio.run(through_a_socket()) == [STANDING_URI]
