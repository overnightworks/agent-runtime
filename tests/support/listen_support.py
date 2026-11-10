"""The arrangement the listen suites share: two members, their app, the changes and the front."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.member import STANDING_URI
from agents.tools import Principal
from tests.support.mcp_server_support import (
    MemberApp,
    a_member,
    readable_uris,
    resolve_member,
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


def served[T](main: Coroutine[Any, Any, T]) -> T:
    return in_virtual_time(main)
