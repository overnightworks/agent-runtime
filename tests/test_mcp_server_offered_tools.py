"""The person front's tools of the moment: each member's offer and its change notice.

The offer is the host's port, read at every request. The front answers only the declared tools it
names, refuses a call outside it as a failed result, and announces a change to the streams that
honored the notice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from mcp.shared.subscriptions import ToolsListChanged

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.asgi import LOGGER_NAME as LOGGER
from agents.member import STANDING_URI
from agents.tools import Principal
from tests.support import listen_support
from tests.support.mcp_person_support import Person
from tests.support.mcp_server_support import (
    ListenConnection,
    MemberApp,
    ToolOffer,
    call_params,
    listen_connection,
    loopback_client,
    loopback_server,
    open_stream,
    post,
    resolve_member,
    rpc_body,
    settled,
)

alice = listen_support.alice
bob = listen_support.bob
app = listen_support.app
changes = listen_support.changes
served = listen_support.served

_TOOLS_CHANGED = "notifications/tools/list_changed"
_ACKNOWLEDGED = "notifications/subscriptions/acknowledged"
_NOT_OFFERED_TEXT = "the tool is not offered now"
_OFFER_FAILED_TEXT = "the tool offer failed"
_DECLARED = ("read_note", "edit_note", "bid")


@pytest.fixture
def person() -> Person:
    return Person()


@pytest.fixture
def offer(alice: Principal, bob: Principal) -> ToolOffer:
    return ToolOffer(offered={alice: ("bid", "read_note"), bob: ()})


@pytest.fixture
def front(
    person: Person, app: MemberApp, changes: ResourceChanges, offer: ToolOffer
) -> McpPersonFront:
    return McpPersonFront(
        [person.tool(name) for name in _DECLARED],
        resolve_person=resolve_member,
        resources=app,
        changes=changes,
        offered_tools=offer,
    )


async def answer(front: McpPersonFront, member: Principal, body: bytes) -> dict[str, Any]:
    """The JSON-RPC message the front answers ``body`` with."""
    async with front.running():
        sent = await post(front, member, body)
    return json.loads(b"".join(message.get("body", b"") for message in sent))


async def listed(front: McpPersonFront, member: Principal) -> dict[str, Any]:
    return (await answer(front, member, rpc_body("tools/list")))["result"]


async def called(front: McpPersonFront, member: Principal, name: str, **arguments: Any) -> Any:
    return await answer(front, member, rpc_body("tools/call", call_params(name, **arguments)))


def names_of(result: dict[str, Any]) -> list[str]:
    return [tool["name"] for tool in result["tools"]]


def test_tools_list_answers_only_the_offered_tools_in_declaration_order_per_member(
    front: McpPersonFront, alice: Principal, bob: Principal
) -> None:
    async def both_at_once() -> list[dict[str, Any]]:
        return await asyncio.gather(listed(front, alice), listed(front, bob))

    alice_list, bob_list = served(both_at_once())

    assert names_of(alice_list) == ["read_note", "bid"]
    assert names_of(bob_list) == []
    assert [(each["ttlMs"], each["cacheScope"]) for each in (alice_list, bob_list)] == [
        (0, "private")
    ] * 2


def test_a_change_of_the_offer_shows_at_the_next_list_and_the_next_call(
    front: McpPersonFront, person: Person, offer: ToolOffer, alice: Principal
) -> None:
    async def scenario() -> tuple[list[str], list[str], bool, bool]:
        before = names_of(await listed(front, alice))
        before_call = "result" in await called(front, alice, "bid")
        offer.offered[alice] = ("edit_note",)
        after = names_of(await listed(front, alice))
        after_call = (await called(front, alice, "bid"))["result"]["isError"]
        return before, after, before_call, after_call

    before, after, before_call, after_call = served(scenario())

    assert (before, after) == (["read_note", "bid"], ["edit_note"])
    assert (before_call, after_call) == (True, True)
    assert [name for name, _request, _arguments in person.calls] == ["bid"]


@pytest.mark.parametrize(
    "arguments",
    [{"note": "n1"}, {"note": "lone \ud800"}],
    ids=["valid arguments", "arguments no handler may receive"],
)
def test_a_call_to_a_declared_tool_outside_the_offer_runs_nothing_and_answers_not_offered(
    front: McpPersonFront, person: Person, alice: Principal, arguments: dict[str, Any]
) -> None:
    reply = served(called(front, alice, "edit_note", **arguments))

    assert reply["result"]["isError"] is True
    assert reply["result"]["content"] == [{"type": "text", "text": _NOT_OFFERED_TEXT}]
    assert person.calls == []


def test_a_call_to_an_undeclared_tool_answers_unknown_tool_without_asking_the_offer(
    front: McpPersonFront, offer: ToolOffer, alice: Principal
) -> None:
    reply = served(called(front, alice, "never_declared"))

    assert (reply["error"]["code"], reply["error"]["message"]) == (-32602, "Unknown tool")
    assert offer.asked == []


def test_a_call_with_arguments_asks_the_offer_exactly_once(
    front: McpPersonFront, person: Person, offer: ToolOffer, alice: Principal
) -> None:
    reply = served(called(front, alice, "bid", note="x"))

    assert reply["result"]["isError"] is False
    assert offer.asked == [alice]
    assert [name for name, _request, _arguments in person.calls] == ["bid"]


@pytest.mark.parametrize(
    "breakage",
    [
        pytest.param({"failure": RuntimeError("secret-marker")}, id="raises"),
        pytest.param({"unusable": ("bid", "never_declared")}, id="undeclared name"),
        pytest.param({"unusable": "bid"}, id="bare text"),
        pytest.param({"unusable": b"bid"}, id="bare bytes"),
        pytest.param({"unusable": 7}, id="no collection"),
        pytest.param({"unusable": ("bid", 7)}, id="non-text item"),
        pytest.param({"unusable": ([],)}, id="unhashable item"),
    ],
)
@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
def test_a_broken_offer_answers_one_fixed_failure_and_logs_only_the_exception_type(
    front: McpPersonFront,
    person: Person,
    offer: ToolOffer,
    alice: Principal,
    caplog: pytest.LogCaptureFixture,
    method: str,
    breakage: dict[str, Any],
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    for field_name, value in breakage.items():
        setattr(offer, field_name, value)
    params = call_params("bid") if method == "tools/call" else None

    reply = served(answer(front, alice, rpc_body(method, params)))

    assert (reply["error"]["code"], reply["error"]["message"]) == (-32603, _OFFER_FAILED_TEXT)
    assert person.calls == []
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert method in logged
    assert "secret-marker" not in logged + json.dumps(reply)
    assert "never_declared" not in logged + json.dumps(reply)


def test_offered_tools_without_changes_are_refused_at_construction(
    person: Person, app: MemberApp, offer: ToolOffer
) -> None:
    with pytest.raises(ValueError, match="changes"):
        McpPersonFront(
            [person.tool("bid")], resolve_person=resolve_member, resources=app, offered_tools=offer
        )


async def discovered_capabilities(front: McpPersonFront, member: Principal) -> dict[str, Any]:
    return (await answer(front, member, rpc_body("server/discover", {})))["result"]["capabilities"]


def test_discovery_declares_tools_list_changed_with_the_offer_and_no_resources_list_changed(
    front: McpPersonFront, alice: Principal
) -> None:
    capabilities = served(discovered_capabilities(front, alice))

    assert capabilities["tools"]["listChanged"] is True
    assert capabilities["resources"] == {"subscribe": True, "listChanged": False}


@pytest.mark.parametrize(
    ("flags", "honored"),
    [
        pytest.param({"toolsListChanged": True}, {"toolsListChanged": True}, id="asked"),
        pytest.param({}, {}, id="not asked"),
        pytest.param({"toolsListChanged": False}, {}, id="refused"),
        pytest.param(
            {"promptsListChanged": True, "resourcesListChanged": True},
            {},
            id="no other list-changed flag",
        ),
    ],
)
def test_a_listen_honors_tools_list_changed_only_when_asked(
    front: McpPersonFront, alice: Principal, flags: dict[str, bool], honored: dict[str, Any]
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI], flags=flags)

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            pass

    served(scenario())

    acknowledged = connection.frames[0]["params"]["notifications"]
    assert acknowledged == {"resourceSubscriptions": [STANDING_URI], **honored}


def tools_frames(connection: ListenConnection) -> list[dict[str, Any]]:
    return [frame for frame in connection.frames if frame.get("method") == _TOOLS_CHANGED]


def test_a_tools_change_reaches_each_stream_of_its_member_that_honored_it_and_no_other(
    front: McpPersonFront, alice: Principal, bob: Principal, changes: ResourceChanges
) -> None:
    asking = {"toolsListChanged": True}
    streams = {
        "alice tools": listen_connection(front, alice, [STANDING_URI], flags=asking, request_id=7),
        "alice uris only": listen_connection(front, alice, [STANDING_URI]),
        "bob tools": listen_connection(front, bob, [STANDING_URI], flags=asking),
    }

    async def scenario() -> None:
        async with (
            front.running(),
            open_stream(streams["alice tools"]),
            open_stream(streams["alice uris only"]),
            open_stream(streams["bob tools"]),
        ):
            changes.tools_changed(alice)
            await settled()

    served(scenario())

    assert {name: len(tools_frames(stream)) for name, stream in streams.items()} == {
        "alice tools": 1,
        "alice uris only": 0,
        "bob tools": 0,
    }
    [frame] = tools_frames(streams["alice tools"])
    assert frame["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == 7


@pytest.mark.parametrize(
    "signal_moment",
    ["while the port answers", "after the acknowledgement was written"],
)
def test_a_tools_change_around_the_acknowledgement_arrives_after_it(
    front: McpPersonFront,
    app: MemberApp,
    alice: Principal,
    changes: ResourceChanges,
    signal_moment: str,
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI], flags={"toolsListChanged": True})

    def signal_once() -> None:
        app.before_current = lambda: None
        changes.tools_changed(alice)

    async def scenario() -> None:
        if signal_moment == "while the port answers":
            app.before_current = signal_once
        async with front.running(), open_stream(connection):
            if signal_moment != "while the port answers":
                signal_once()
            await settled()

    served(scenario())

    assert [frame.get("method") for frame in connection.frames] == [_ACKNOWLEDGED, _TOOLS_CHANGED]


def test_repeated_tools_changes_are_written_once_beside_resource_changes_in_arrival_order(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(
        front, alice, [STANDING_URI], flags={"toolsListChanged": True}, stall_at=1
    )

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.tools_changed(alice)
            changes.changed(alice, STANDING_URI)
            changes.tools_changed(alice)
            await settled()
            connection.release()
            await settled()

    served(scenario())

    assert [frame.get("method") for frame in connection.frames] == [
        _ACKNOWLEDGED,
        _TOOLS_CHANGED,
        "notifications/resources/updated",
    ]


def test_a_stream_honoring_only_tools_counts_for_no_uri_and_signals_no_listening(
    app: MemberApp, person: Person, offer: ToolOffer, alice: Principal
) -> None:
    heard: list[tuple[Principal, str, bool]] = []
    changes = ResourceChanges(lambda *args: heard.append(args))
    front = McpPersonFront(
        [person.tool("bid")],
        resolve_person=resolve_member,
        resources=app,
        changes=changes,
        offered_tools=offer,
    )
    connection = listen_connection(front, alice, [], flags={"toolsListChanged": True})
    during: list[bool] = []

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            during.append(changes.listens_to(alice, STANDING_URI))
            await asyncio.sleep(1)

    served(scenario())

    assert during == [False]
    assert heard == []


def test_the_sdk_client_hears_the_change_lists_the_new_offer_and_is_refused_the_old_tool(
    front: McpPersonFront, offer: ToolOffer, alice: Principal, changes: ResourceChanges
) -> None:
    async def through_a_socket() -> tuple[bool | None, list[str], list[str], bool]:
        async with loopback_client(loopback_server(front), alice) as client:
            before = [tool.name for tool in (await client.list_tools()).tools]
            async with client.listen(
                tools_list_changed=True, resource_subscriptions=[STANDING_URI]
            ) as stream:
                honored = stream.honored.tools_list_changed
                offer.offered[alice] = ("edit_note",)
                changes.tools_changed(alice)
                async for event in stream:
                    assert isinstance(event, ToolsListChanged)
                    break
            after = [tool.name for tool in (await client.list_tools()).tools]
            refused = (await client.call_tool("bid", {})).is_error
        return honored, before, after, refused

    honored, before, after, refused = asyncio.run(through_a_socket())

    assert (honored, before, after, refused) == (True, ["read_note", "bid"], ["edit_note"], True)
