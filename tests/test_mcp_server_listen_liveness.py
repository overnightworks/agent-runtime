"""Whether a principal listens to a URI: the answer ``ResourceChanges.listens_to`` gives an app."""

from __future__ import annotations

import asyncio

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.member import LISTEN_BOUND_SECONDS, STANDING_URI
from agents.tools import Principal
from tests.support import listen_support
from tests.support.mcp_server_support import (
    MemberApp,
    listen_connection,
    open_stream,
    window_uri,
)

alice = listen_support.alice
bob = listen_support.bob
app = listen_support.app
changes = listen_support.changes
front = listen_support.front
served = listen_support.served


@pytest.mark.parametrize(
    ("requested", "listens"),
    [
        pytest.param([STANDING_URI], True, id="the standing is acknowledged"),
        pytest.param(
            [window_uri("first"), STANDING_URI], True, id="the standing among other places"
        ),
        pytest.param([window_uri("first")], False, id="only a place window"),
        pytest.param(
            [window_uri("foreign")], False, id="a requested URI the principal may not read"
        ),
        pytest.param([], False, id="nothing requested"),
    ],
)
def test_a_principal_listens_to_the_standing_when_an_acknowledged_stream_covers_it(
    front: McpPersonFront,
    alice: Principal,
    changes: ResourceChanges,
    requested: list[str],
    listens: bool,
) -> None:
    connection = listen_connection(front, alice, requested)

    async def scenario() -> bool:
        async with front.running(), open_stream(connection):
            return changes.listens_to(alice, STANDING_URI)

    assert served(scenario()) is listens


def test_another_member_listening_to_the_standing_does_not_make_a_principal_listen(
    front: McpPersonFront, alice: Principal, bob: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, bob, [STANDING_URI])

    async def scenario() -> bool:
        async with front.running(), open_stream(connection):
            return changes.listens_to(alice, STANDING_URI)

    assert served(scenario()) is False


def test_a_stream_covers_the_standing_only_once_it_is_acknowledged(
    front: McpPersonFront, app: MemberApp, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])
    app.current_seconds = 5

    async def scenario() -> tuple[bool, bool]:
        async with front.running(), open_stream(connection):
            before = changes.listens_to(alice, STANDING_URI)
            await asyncio.sleep(5)
            return before, changes.listens_to(alice, STANDING_URI)

    assert served(scenario()) == (False, True)


@pytest.mark.parametrize(
    "staying_seconds",
    [
        pytest.param(0, id="the client leaves"),
        pytest.param(LISTEN_BOUND_SECONDS * 2, id="the bound"),
    ],
)
def test_an_ended_stream_no_longer_covers_the_standing(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges, staying_seconds: float
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> tuple[bool, bool]:
        async with front.running():
            async with open_stream(connection):
                await asyncio.sleep(staying_seconds)
                at_the_end = changes.listens_to(alice, STANDING_URI)
            return at_the_end, changes.listens_to(alice, STANDING_URI)

    listening_while_open = staying_seconds == 0
    assert served(scenario()) == (listening_while_open, False)


def test_several_streams_of_one_principal_cover_the_standing_while_any_of_them_does(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    place_only = listen_connection(front, alice, [window_uri("first")])
    standing = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> list[bool]:
        answers = []
        async with front.running(), open_stream(place_only):
            async with open_stream(standing):
                answers.append(changes.listens_to(alice, STANDING_URI))
            answers.append(changes.listens_to(alice, STANDING_URI))
        return answers

    assert served(scenario()) == [True, False]
