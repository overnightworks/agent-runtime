"""The wait tool through the person front: every answer is the member's whole current standing.

Every call runs on a loop whose clock jumps to its next timer, so no test waits real seconds;
``ToolResult.seconds`` is the time the call took on that clock.
"""

from __future__ import annotations

import asyncio
import tracemalloc
from typing import Any

import anyio
import pytest

from agent_runtime_client.mcp_server import InMemoryMemberChanges
from agent_runtime_client.tools import Principal
from agent_runtime_client.wait import STANDINGS_READ_SECONDS, WAIT_BOUND_SECONDS, StandingState
from tests.support.mcp_server_support import (
    WaitScenario,
    a_member,
    a_standing,
    in_virtual_time,
    post,
    result_of,
    tool_call_body,
)

INVITED = a_standing("p-1", StandingState.INVITED)
ACTIVE = a_standing("p-2", StandingState.ACTIVE)
PAUSED = a_standing("p-2", StandingState.PAUSED)


@pytest.fixture
def scenario() -> WaitScenario:
    world = WaitScenario()
    world.places.standings[a_member()] = (INVITED, ACTIVE)
    return world


async def current_cursor(scenario: WaitScenario, member: Principal) -> str:
    return (await scenario.wait(member)).answer().cursor


async def change_after(scenario: WaitScenario, seconds: float, member: Principal, *standings):
    await asyncio.sleep(seconds)
    scenario.change(member, *standings)


def test_a_first_wait_answers_at_once_with_every_standing_and_a_cursor(
    scenario: WaitScenario,
) -> None:
    result = in_virtual_time(scenario.wait(a_member()))

    assert (result.is_error, result.seconds) == (False, pytest.approx(0, abs=0.01))
    assert result.answer().standings == (INVITED, ACTIVE)
    assert result.answer().cursor


def test_a_wait_with_the_current_cursor_answers_the_unchanged_standing_at_its_timeout_each_time(
    scenario: WaitScenario,
) -> None:
    async def twice() -> list[Any]:
        cursor = await current_cursor(scenario, a_member())
        return [await scenario.wait(a_member(), after=cursor, timeout_seconds=1) for _ in "ab"]

    first, retried = in_virtual_time(twice())

    for result in (first, retried):
        assert result.seconds == pytest.approx(1, abs=0.01)
        assert result.answer().standings == (INVITED, ACTIVE)
    assert first.answer().cursor == retried.answer().cursor


def test_a_signalled_change_ends_the_wait_before_its_timeout_with_the_new_standing(
    scenario: WaitScenario,
) -> None:
    async def waiting() -> Any:
        cursor = await current_cursor(scenario, a_member())
        async with anyio.create_task_group() as group:
            group.start_soon(change_after, scenario, 10, a_member(), PAUSED)
            return await scenario.wait(a_member(), after=cursor)

    result = in_virtual_time(waiting())

    assert result.seconds == pytest.approx(10, abs=0.01)
    assert result.answer().standings == (PAUSED,)


@pytest.mark.parametrize("yields", range(12))
def test_a_change_signalled_at_any_step_of_a_starting_wait_is_never_lost(
    scenario: WaitScenario, yields: int
) -> None:
    async def change_after_yields() -> None:
        for _ in range(yields):
            await asyncio.sleep(0)
        scenario.change(a_member(), PAUSED)

    async def racing() -> Any:
        cursor = await current_cursor(scenario, a_member())
        async with anyio.create_task_group() as group:
            group.start_soon(change_after_yields)
            return await scenario.wait(a_member(), after=cursor)

    result = in_virtual_time(racing())

    assert result.seconds == pytest.approx(0, abs=0.01)
    assert result.answer().standings == (PAUSED,)


@pytest.mark.parametrize("moment", ["before_taking", "after_taking"])
def test_a_change_during_the_read_makes_the_next_wait_answer_at_once_with_the_new_standing(
    scenario: WaitScenario, moment: str
) -> None:
    async def sequential() -> Any:
        cursor = await current_cursor(scenario, a_member())
        setattr(scenario.places, moment, lambda: scenario.change(a_member(), PAUSED))
        delivered = await scenario.wait(a_member(), after=cursor, timeout_seconds=1)
        setattr(scenario.places, moment, lambda: None)
        return await scenario.wait(a_member(), after=delivered.answer().cursor)

    result = in_virtual_time(sequential())

    assert result.seconds == pytest.approx(0, abs=0.01)
    assert result.answer().standings == (PAUSED,)


def test_a_retry_with_a_cursor_older_than_the_latest_change_answers_at_once(
    scenario: WaitScenario,
) -> None:
    async def retried() -> Any:
        lost = await current_cursor(scenario, a_member())
        scenario.change(a_member(), PAUSED)
        return await scenario.wait(a_member(), after=lost)

    result = in_virtual_time(retried())

    assert result.seconds == pytest.approx(0, abs=0.01)
    assert result.answer().standings == (PAUSED,)


def test_a_cursor_of_an_earlier_run_answers_at_once_after_a_restart(
    scenario: WaitScenario,
) -> None:
    async def restarted() -> Any:
        cursor = await current_cursor(scenario, a_member())
        scenario.changes = InMemoryMemberChanges()
        return await scenario.wait(a_member(), after=cursor)

    result = in_virtual_time(restarted())

    assert result.seconds == pytest.approx(0, abs=0.01)
    assert result.answer().standings == (INVITED, ACTIVE)


@pytest.mark.parametrize("after", ["garbage", "", "x" * 128], ids=["garbage", "empty", "longest"])
def test_an_admitted_string_that_is_no_cursor_answers_at_once(
    scenario: WaitScenario, after: str
) -> None:
    result = in_virtual_time(scenario.wait(a_member(), after=after))

    assert result.seconds == pytest.approx(0, abs=0.01)
    assert result.answer().standings == (INVITED, ACTIVE)


def test_another_members_cursor_answers_only_the_callers_own_standing(
    scenario: WaitScenario,
) -> None:
    other = a_member("bob")

    async def borrowed() -> Any:
        scenario.change(other, a_standing("p-9", StandingState.ACTIVE))
        return await scenario.wait(a_member(), after=await current_cursor(scenario, other))

    result = in_virtual_time(borrowed())

    assert result.seconds == pytest.approx(0, abs=0.01)
    assert result.answer().standings == (INVITED, ACTIVE)


def test_one_subject_at_two_issuers_is_two_members_that_wake_and_read_apart(
    scenario: WaitScenario,
) -> None:
    here, there = a_member("s-1", "https://one.test"), a_member("s-1", "https://two.test")
    scenario.places.standings |= {here: (ACTIVE,), there: (INVITED,)}

    async def both() -> list[Any]:
        cursors = [await current_cursor(scenario, member) for member in (here, there)]
        results: list[Any] = [None, None]

        async def wait_as(index: int, member: Principal) -> None:
            results[index] = await scenario.wait(member, after=cursors[index])

        async with anyio.create_task_group() as group:
            group.start_soon(wait_as, 0, here)
            group.start_soon(wait_as, 1, there)
            group.start_soon(change_after, scenario, 5, here, PAUSED)
        return results

    woken, untouched = in_virtual_time(both())

    assert (woken.seconds, woken.answer().standings) == (pytest.approx(5, abs=0.01), (PAUSED,))
    assert untouched.seconds == pytest.approx(WAIT_BOUND_SECONDS, abs=0.01)
    assert untouched.answer().standings == (INVITED,)


@pytest.mark.parametrize(
    ("arguments", "refused"),
    [
        ({"after": "x" * 129}, "after"),
        ({"after": 5}, "after"),
        ({"after": None}, "after"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": WAIT_BOUND_SECONDS + 1}, "timeout_seconds"),
        ({"timeout_seconds": 1.5}, "timeout_seconds"),
        ({"timeout_seconds": "10"}, "timeout_seconds"),
        ({"timeout_seconds": True}, "timeout_seconds"),
        ({"recipient": "bob"}, "recipient"),
    ],
    ids=[
        "after-too-long",
        "after-a-number",
        "after-null",
        "timeout-zero",
        "timeout-past-the-bound",
        "timeout-fractional",
        "timeout-a-string",
        "timeout-a-boolean",
        "extra-argument",
    ],
)
def test_a_refused_argument_is_the_tools_error_naming_it_with_no_standing(
    scenario: WaitScenario, arguments: dict[str, Any], refused: str
) -> None:
    result = in_virtual_time(scenario.wait(a_member(), **arguments))

    assert (result.is_error, result.seconds) == (True, pytest.approx(0, abs=0.01))
    assert refused in result.text
    assert "p-1" not in result.text


def test_a_wait_without_a_timeout_ends_at_the_bound_plus_the_read(
    scenario: WaitScenario,
) -> None:
    scenario.places.read_seconds = 2

    async def bounded() -> Any:
        cursor = await current_cursor(scenario, a_member())
        return await scenario.wait(a_member(), after=cursor)

    result = in_virtual_time(bounded())

    assert result.seconds == pytest.approx(WAIT_BOUND_SECONDS + 2, abs=0.01)
    assert result.answer().standings == (INVITED, ACTIVE)


def test_a_failing_read_is_the_tools_error_and_the_front_answers_the_next_call(
    scenario: WaitScenario,
) -> None:
    async def failing_then_fixed() -> list[Any]:
        scenario.places.failure = LookupError("the store is down")
        failed = await scenario.wait(a_member())
        scenario.places.failure = None
        return [failed, await scenario.wait(a_member())]

    failed, recovered = in_virtual_time(failing_then_fixed())

    assert failed.is_error
    assert "p-1" not in failed.text
    assert recovered.answer().standings == (INVITED, ACTIVE)


@pytest.mark.parametrize("current", [False, True], ids=["stale-cursor", "current-cursor"])
def test_a_read_past_its_deadline_is_the_tools_error_within_the_timeout_plus_the_deadline(
    scenario: WaitScenario, current: bool
) -> None:
    async def slow() -> Any:
        arguments = {"after": await current_cursor(scenario, a_member())} if current else {}
        scenario.places.read_seconds = 3600
        return await scenario.wait(a_member(), timeout_seconds=1, **arguments)

    result = in_virtual_time(slow())

    waited = 1 if current else 0
    assert result.is_error
    assert result.seconds == pytest.approx(waited + STANDINGS_READ_SECONDS, abs=0.01)


@pytest.mark.parametrize(
    ("alongside", "woken_after"), [(False, 3), (True, 13)], ids=["then", "beside-another-wait"]
)
def test_a_cancelled_wait_sends_nothing_and_leaves_every_other_wait_of_the_member_wakeable(
    scenario: WaitScenario, alongside: bool, woken_after: float
) -> None:
    async def cancelled_then_changed() -> tuple[list[Any], list[Any]]:
        cursor = await current_cursor(scenario, a_member())
        sent: list[Any] = []
        results: list[Any] = []

        async def cancelled() -> None:
            body = tool_call_body("wait", {"after": cursor})
            sent.extend(await post(scenario.front, a_member(), body))

        async def waiting() -> None:
            results.append(await scenario.wait(a_member(), after=cursor))

        async with anyio.create_task_group() as group:
            if alongside:
                group.start_soon(waiting)
            async with anyio.create_task_group() as doomed:
                doomed.start_soon(cancelled)
                await asyncio.sleep(10)
                doomed.cancel_scope.cancel()
            if not alongside:
                group.start_soon(waiting)
            group.start_soon(change_after, scenario, 3, a_member(), PAUSED)
        return sent, results

    sent, [result] = in_virtual_time(cancelled_then_changed())

    assert sent == []
    assert (result.seconds, result.answer().standings) == (
        pytest.approx(woken_after, abs=0.01),
        (PAUSED,),
    )


def test_two_waits_of_one_member_both_answer_one_change(scenario: WaitScenario) -> None:
    async def both() -> list[Any]:
        cursor = await current_cursor(scenario, a_member())
        results: list[Any] = []

        async def waiting() -> None:
            results.append(await scenario.wait(a_member(), after=cursor))

        async with anyio.create_task_group() as group:
            group.start_soon(waiting)
            group.start_soon(waiting)
            group.start_soon(change_after, scenario, 4, a_member(), PAUSED)
        return results

    results = in_virtual_time(both())

    assert [(r.seconds, r.answer().standings) for r in results] == [
        (pytest.approx(4, abs=0.01), (PAUSED,)),
    ] * 2


def test_a_paused_place_is_listed_and_a_place_left_is_absent(scenario: WaitScenario) -> None:
    async def paused_then_removed() -> list[Any]:
        scenario.change(a_member(), INVITED, PAUSED)
        paused = await scenario.wait(a_member())
        scenario.change(a_member(), INVITED)
        return [paused, await scenario.wait(a_member(), after=paused.answer().cursor)]

    paused, removed = in_virtual_time(paused_then_removed())

    assert paused.answer().standings == (INVITED, PAUSED)
    assert (removed.seconds, removed.answer().standings) == (pytest.approx(0, abs=0.01), (INVITED,))


def test_the_listed_wait_tool_is_read_only_and_names_no_recipient(scenario: WaitScenario) -> None:
    body = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'

    [listed] = result_of(in_virtual_time(post(scenario.front, a_member(), body)))["tools"]

    assert (listed["name"], listed["annotations"]) == ("wait", {"readOnlyHint": True})
    assert listed["inputSchema"]["additionalProperties"] is False
    assert set(listed["inputSchema"]["properties"]) == {"after", "timeout_seconds"}


def test_the_in_memory_changes_grow_with_neither_changes_nor_finished_waits() -> None:
    changes = InMemoryMemberChanges()
    members = [a_member(f"member-{number}") for number in range(1000)]

    async def changed_and_waited() -> int:
        for member in members:
            changes.changed(member)
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        for member in members:
            for _ in range(10):
                changes.changed(member)
            cursor = await changes.wait(member, None, 1)
            await changes.wait(member, cursor, 1)
        grown = tracemalloc.get_traced_memory()[0] - before
        tracemalloc.stop()
        return grown

    assert in_virtual_time(changed_and_waited()) < 32 * 1024
