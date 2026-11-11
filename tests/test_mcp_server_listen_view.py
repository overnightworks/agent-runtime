"""A place's view on the person front's listen: a standing subscriber is woken for it."""

from __future__ import annotations

from collections.abc import Callable

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


def woken_by(
    front: McpPersonFront,
    changes: ResourceChanges,
    member: Principal,
    listened: str,
    signalled: list[str],
    after_signal: Callable[[int], None] = lambda _index: None,
) -> list[str]:
    """The URIs a listen for ``listened`` delivers when each of ``signalled`` is signalled, and
    ``after_signal`` runs after each, before the stream drains it."""
    connection = listen_connection(front, member, [listened])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            for index, uri in enumerate(signalled):
                changes.changed(member, uri)
                after_signal(index)
                await settled()

    served(scenario())
    return connection.updated_uris


@pytest.mark.usefixtures("viewing_app")
@pytest.mark.parametrize(
    ("listened", "signalled", "woken"),
    [
        pytest.param(
            STANDING_URI,
            [view_uri("first")],
            [view_uri("first")],
            id="a standing subscriber and the app signals a view",
        ),
        pytest.param(
            view_uri("first"),
            [STANDING_URI, window_uri("first"), view_uri("first")],
            [view_uri("first")],
            id="a subscriber of one view gets that view alone",
        ),
        pytest.param(
            STANDING_URI,
            [view_uri("second")],
            [],
            id="a view the app does not list among the member's resources",
        ),
    ],
)
def test_a_view_wakes_its_subscribers_when_the_app_lists_it(
    front: McpPersonFront,
    alice: Principal,
    changes: ResourceChanges,
    listened: str,
    signalled: list[str],
    woken: list[str],
) -> None:
    assert woken_by(front, changes, alice, listened, signalled) == woken


@pytest.mark.parametrize(
    ("removed_at", "woken"),
    [(0, []), (1, [view_uri("first")])],
    ids=["removed with the first pulse queued", "removed with the second pulse queued"],
)
def test_delivery_for_a_view_ends_when_the_member_is_removed_from_the_place(
    front: McpPersonFront,
    viewing_app: MemberApp,
    alice: Principal,
    changes: ResourceChanges,
    removed_at: int,
    woken: list[str],
) -> None:
    def remove_at_the_pulse(index: int) -> None:
        if index == removed_at:
            del viewing_app.readable[alice][view_uri("first")]

    uris = [view_uri("first")] * 2

    assert woken_by(front, changes, alice, STANDING_URI, uris, remove_at_the_pulse) == woken
