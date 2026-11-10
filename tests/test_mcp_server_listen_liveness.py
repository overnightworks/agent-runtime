"""Whether a principal listens to a URI: the answer ``ResourceChanges.listens_to`` gives an app."""

from __future__ import annotations

import asyncio

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.listen import LISTEN_ALLOWANCE_SECONDS
from agents.member import LISTEN_BOUND_SECONDS, STANDING_URI
from agents.tools import Principal
from tests.support import listen_support
from tests.support.mcp_server_support import (
    MemberApp,
    listen_connection,
    open_stream,
    resolve_member,
    settled,
    window_uri,
)

alice = listen_support.alice
bob = listen_support.bob
app = listen_support.app
changes = listen_support.changes
front = listen_support.front
served = listen_support.served

type Signal = tuple[Principal, str, bool]
_PAST_THE_ALLOWANCE = LISTEN_ALLOWANCE_SECONDS + 1


@pytest.fixture
def signals() -> list[Signal]:
    return []


@pytest.fixture
def signalling_changes(signals: list[Signal]) -> ResourceChanges:
    return ResourceChanges(
        lambda principal, uri, listens: signals.append((principal, uri, listens))
    )


@pytest.fixture
def signalling_front(app: MemberApp, signalling_changes: ResourceChanges) -> McpPersonFront:
    return McpPersonFront(
        [], resolve_person=resolve_member, resources=app, changes=signalling_changes
    )


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
def test_an_ended_stream_no_longer_covers_the_standing_once_the_allowance_has_passed(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges, staying_seconds: float
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> bool:
        async with front.running():
            async with open_stream(connection):
                await asyncio.sleep(staying_seconds)
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            return changes.listens_to(alice, STANDING_URI)

    assert served(scenario()) is False


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
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            answers.append(changes.listens_to(alice, STANDING_URI))
        return answers

    assert served(scenario()) == [True, False]


def test_a_reopened_listen_within_the_allowance_keeps_the_answer_yes_and_signals_nothing_more(
    signalling_front: McpPersonFront,
    signalling_changes: ResourceChanges,
    signals: list[Signal],
    alice: Principal,
) -> None:
    first = listen_connection(signalling_front, alice, [STANDING_URI])
    second = listen_connection(signalling_front, alice, [STANDING_URI], request_id=2)

    async def scenario() -> list[bool]:
        answers = []
        async with signalling_front.running():
            async with open_stream(first):
                pass
            answers.append(signalling_changes.listens_to(alice, STANDING_URI))
            await asyncio.sleep(LISTEN_ALLOWANCE_SECONDS / 2)
            async with open_stream(second):
                answers.append(signalling_changes.listens_to(alice, STANDING_URI))
                await asyncio.sleep(_PAST_THE_ALLOWANCE)
                answers.append(signalling_changes.listens_to(alice, STANDING_URI))
        return answers

    assert served(scenario()) == [True, True, True]
    assert signals == [(alice, STANDING_URI, True)]


@pytest.mark.parametrize(
    "staying_seconds",
    [
        pytest.param(0, id="the client leaves"),
        pytest.param(LISTEN_BOUND_SECONDS * 2, id="the bound"),
    ],
)
def test_a_listen_nothing_follows_turns_the_answer_no_after_the_allowance_with_one_signal(
    signalling_front: McpPersonFront,
    signalling_changes: ResourceChanges,
    signals: list[Signal],
    alice: Principal,
    staying_seconds: float,
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> list[bool]:
        answers = []
        async with signalling_front.running():
            async with open_stream(connection):
                await asyncio.sleep(staying_seconds)
            await asyncio.sleep(LISTEN_ALLOWANCE_SECONDS / 2)
            answers.append(signalling_changes.listens_to(alice, STANDING_URI))
            await asyncio.sleep(LISTEN_ALLOWANCE_SECONDS)
            answers.append(signalling_changes.listens_to(alice, STANDING_URI))
        return answers

    answers = served(scenario())

    assert answers[-1] is False
    assert signals == [(alice, STANDING_URI, True), (alice, STANDING_URI, False)]


def test_the_first_covering_stream_signals_yes_and_a_second_signals_nothing(
    signalling_front: McpPersonFront, signals: list[Signal], alice: Principal, bob: Principal
) -> None:
    streams = [
        listen_connection(signalling_front, alice, [STANDING_URI], request_id=1),
        listen_connection(signalling_front, alice, [STANDING_URI], request_id=2),
        listen_connection(signalling_front, bob, [STANDING_URI], request_id=3),
    ]

    async def scenario() -> list[list[Signal]]:
        seen = []
        async with signalling_front.running(), open_stream(streams[0]):
            seen.append(list(signals))
            async with open_stream(streams[1]):
                seen.append(list(signals))
                async with open_stream(streams[2]):
                    seen.append(list(signals))
        return seen

    assert served(scenario()) == [
        [(alice, STANDING_URI, True)],
        [(alice, STANDING_URI, True)],
        [(alice, STANDING_URI, True), (bob, STANDING_URI, True)],
    ]


def test_a_stream_signals_only_once_acknowledged(
    signalling_front: McpPersonFront, app: MemberApp, signals: list[Signal], alice: Principal
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])
    app.current_seconds = 5

    async def scenario() -> tuple[list[Signal], list[Signal]]:
        async with signalling_front.running(), open_stream(connection):
            before = list(signals)
            await asyncio.sleep(5)
            return before, list(signals)

    assert served(scenario()) == ([], [(alice, STANDING_URI, True)])


def test_a_callable_that_raises_leaves_the_listen_unharmed(
    app: MemberApp, alice: Principal
) -> None:
    def raises(_principal: Principal, _uri: str, _listens: bool) -> None:
        raise RuntimeError("the host's callable failed")

    changes = ResourceChanges(raises)
    front = McpPersonFront([], resolve_person=resolve_member, resources=app, changes=changes)
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> list[bool]:
        answers = []
        async with front.running():
            async with open_stream(connection):
                answers.append(changes.listens_to(alice, STANDING_URI))
                changes.changed(alice, STANDING_URI)
                await settled()
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            answers.append(changes.listens_to(alice, STANDING_URI))
        return answers

    assert served(scenario()) == [True, False]
    assert connection.updated_uris == [STANDING_URI]
    assert connection.status == 200
