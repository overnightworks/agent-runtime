"""Codex CLI process admission tests."""

from __future__ import annotations

import pytest
from tests.support.provider_test_support import override_codex_turn_runtime, override_turn_runtime

from agent_runtime.codex.pool import (
    CodexProcessPool,
    get_codex_process_pool,
)
from agent_runtime.config import ProviderNotConfiguredError
from agent_runtime.errors import CodexProcessPoolSaturatedError


def test_the_process_wide_pool_admits_only_as_many_runs_as_the_runtime_allows() -> None:
    override_codex_turn_runtime(max_concurrent_processes=1)

    pool = get_codex_process_pool()
    pool.reserve()

    with pytest.raises(CodexProcessPoolSaturatedError):
        pool.reserve()


def test_every_caller_shares_the_one_process_wide_pool() -> None:
    assert get_codex_process_pool() is get_codex_process_pool()



def test_total_cap_counts_bound_and_unspawned_reservations() -> None:
    pool = CodexProcessPool(maximum_processes=2)
    pool.bind(pool.reserve(), 42)
    pool.reserve()

    with pytest.raises(CodexProcessPoolSaturatedError):
        pool.reserve()


def test_reservation_remains_held_from_bind_until_reap() -> None:
    pool = CodexProcessPool(maximum_processes=1)
    reservation = pool.reserve()
    pool.bind(reservation, 42)

    with pytest.raises(CodexProcessPoolSaturatedError):
        pool.reserve()

    pool.reap(reservation, 42)
    pool.reserve()


def test_unspawned_reservation_is_released_after_spawn_failure() -> None:
    pool = CodexProcessPool(maximum_processes=1)
    reservation = pool.reserve()

    pool.abandon_unspawned(reservation)

    pool.reserve()


def test_zombie_reservation_blocks_new_processes_until_background_reap() -> None:
    pool = CodexProcessPool(maximum_processes=1)
    reservation = pool.reserve()
    pool.bind(reservation, 99)

    with pytest.raises(CodexProcessPoolSaturatedError):
        pool.reserve()

    pool.reap(reservation, 99)
    assert pool.reservation_count() == 0


def test_the_process_wide_pool_without_a_turns_codex_block_is_refused_naming_it() -> None:
    override_turn_runtime(codex=None)

    with pytest.raises(ProviderNotConfiguredError) as refused:
        get_codex_process_pool()

    assert (refused.value.provider, refused.value.block) == ("codex", "turns.codex")
