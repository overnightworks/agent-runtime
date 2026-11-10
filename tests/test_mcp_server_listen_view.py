"""A place's view on the person front's listen: a standing subscriber is woken for it."""

from __future__ import annotations

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.member import STANDING_URI
from agents.tools import Principal
from tests.support import listen_support
from tests.support.mcp_server_support import (
    MemberApp,
    a_view,
    listen_connection,
    open_stream,
    settled,
    view_uri,
    window_uri,
)

alice = listen_support.alice
bob = listen_support.bob
app = listen_support.app
changes = listen_support.changes
front = listen_support.front
served = listen_support.served


@pytest.fixture
def viewing_app(app: MemberApp, alice: Principal) -> MemberApp:
    app.readable[alice][view_uri("first")] = a_view()
    return app


@pytest.mark.usefixtures("viewing_app")
def test_a_standing_subscriber_is_woken_with_the_view_uri_an_app_signals(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, view_uri("first"))
            await settled()

    served(scenario())

    assert connection.updated_uris == [view_uri("first")]


@pytest.mark.usefixtures("viewing_app")
def test_a_subscriber_of_one_view_is_woken_for_that_view_alone(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [view_uri("first")])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, STANDING_URI)
            changes.changed(alice, window_uri("first"))
            changes.changed(alice, view_uri("first"))
            await settled()

    served(scenario())

    assert connection.updated_uris == [view_uri("first")]


def test_a_standing_subscriber_is_not_woken_for_a_view_the_app_does_not_list(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, view_uri("first"))
            await settled()

    served(scenario())

    assert connection.updated_uris == []


@pytest.mark.parametrize(
    "removed_before_the_pulse", [True, False], ids=["queued before", "after the pulse"]
)
def test_delivery_for_a_view_ends_when_the_member_is_removed_from_the_place(
    front: McpPersonFront,
    viewing_app: MemberApp,
    alice: Principal,
    changes: ResourceChanges,
    removed_before_the_pulse: bool,
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            if removed_before_the_pulse:
                changes.changed(alice, view_uri("first"))
                del viewing_app.readable[alice][view_uri("first")]
                await settled()
            else:
                changes.changed(alice, view_uri("first"))
                await settled()
                del viewing_app.readable[alice][view_uri("first")]
                changes.changed(alice, view_uri("first"))
                await settled()

    served(scenario())

    assert connection.updated_uris == ([] if removed_before_the_pulse else [view_uri("first")])


@pytest.mark.usefixtures("viewing_app")
def test_a_member_whose_resources_hold_no_view_is_not_woken_for_it(
    front: McpPersonFront, bob: Principal, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, bob, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            changes.changed(alice, view_uri("first"))
            changes.changed(bob, view_uri("first"))
            await settled()

    served(scenario())

    assert connection.updated_uris == []
