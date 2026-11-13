"""The arrangement the listen suites share: two members, their app, the changes and the front."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.member import STANDING_URI
from agents.tools import Principal
from tests.support.mcp_server_support import (
    ListenConnection,
    MemberApp,
    a_member,
    open_stream,
    readable_uris,
    resolve_member,
    settled,
    window_uri,
)
from tests.support.virtual_time import in_virtual_time


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


async def signalled_around_acknowledgement(
    front: McpPersonFront,
    app: MemberApp,
    connection: ListenConnection,
    signal: Callable[[], None],
    *,
    while_the_port_answers: bool,
) -> None:
    """Opens the stream and signals once, while the port answers the acknowledgement or after it."""

    def signal_once() -> None:
        app.before_current = lambda: None
        signal()

    if while_the_port_answers:
        app.before_current = signal_once
    async with front.running(), open_stream(connection):
        if not while_the_port_answers:
            signal_once()
        await settled()


def served[T](main: Coroutine[Any, Any, T]) -> T:
    return in_virtual_time(main)
