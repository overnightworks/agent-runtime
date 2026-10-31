"""The settings file a ``CodexRun`` writes into its run home, under the operator's levels."""

from __future__ import annotations

import math
import tomllib
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from tests.support.codex_support import DEFAULT_SETTINGS as DEFAULTS
from tests.support.codex_support import FEATURES_OFF
from tests.support.grok_support import a_grok_run
from tests.support.provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    a_codex_login,
)

from agent_runtime.cli_settings import CliSettings, merged
from agent_runtime.codex.run import CodexRun
from agent_runtime.codex_login import assess_codex_login
from agent_runtime.grok.run import GrokRun
from agent_runtime_client.wire import TurnMessage

VALUE_CANARY = "setting-value-canary-3f9a"


def a_run(*settings: CliSettings) -> CodexRun:
    return CodexRun(
        Path("/opt/codex/bin/codex"),
        assess_codex_login(a_codex_login()),
        "system",
        (TurnMessage(role="user", text="question"),),
        model=None,
        settings=settings,
    )


def written_settings(run: CodexRun | GrokRun) -> dict[str, Any]:
    return tomllib.loads(run.run_files()["config.toml"].decode())


def same(left: object, right: object) -> bool:
    """Equality that tells ``True`` from ``1`` and ``1`` from ``1.0``, and NaN from nothing."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(same, left, right))
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left):
        return math.isnan(right)
    return left == right


def test_without_settings_the_file_holds_the_defaults_alone() -> None:
    assert same(written_settings(a_run()), DEFAULTS)


def test_a_codex_run_switches_the_shell_snapshot_off_by_default() -> None:
    assert written_settings(a_run())["features"]["shell_snapshot"] is False


REMOVED_BUILTINS: CliSettings = {
    "features": {"view_image": False, "goals": False, "sleep_tool": False},
    "tools": {"experimental_request_user_input": {"enabled": False}},
    "agents": {"enabled": False},
}


def test_a_codex_run_switches_off_the_built_ins_the_run_does_not_allow() -> None:
    settings = written_settings(a_run())

    assert same(merged(settings, REMOVED_BUILTINS), settings)


def test_an_operator_level_switches_a_removed_built_in_back_on_over_the_default() -> None:
    level: CliSettings = {
        "features": {"view_image": True},
        "tools": {"experimental_request_user_input": {"enabled": True}},
        "agents": {"enabled": True},
    }
    settings = written_settings(a_run(level))

    assert (
        settings["features"]["view_image"],
        settings["tools"]["experimental_request_user_input"]["enabled"],
        settings["agents"]["enabled"],
    ) == (True, True, True)
    assert settings["features"]["goals"] is False


def test_an_operator_level_switches_the_shell_snapshot_on_over_the_default() -> None:
    settings = written_settings(a_run({"features": {"shell_snapshot": True}}))

    assert settings["features"]["shell_snapshot"] is True


def test_one_level_switches_a_feature_on_and_leaves_the_others_off() -> None:
    settings = written_settings(a_run({"features": {"image_generation": True}}))

    assert same(settings["features"], {**DEFAULTS["features"], "image_generation": True})


def test_a_level_overrides_every_default_so_the_operator_wins_over_the_service() -> None:
    level: CliSettings = {
        "sandbox_mode": "workspace-write",
        "approval_policy": "on-request",
        "web_search": "live",
        "mcp_servers": {"x": {"url": "https://x.test"}},
        "features": dict.fromkeys(FEATURES_OFF, True),
        "tools": {"experimental_request_user_input": {"enabled": True}},
        "agents": {"enabled": True},
    }

    assert same(written_settings(a_run(level)), level)


def test_the_levels_merge_in_one_pass_so_a_scalar_cuts_the_defaults_table_below_it() -> None:
    levels: tuple[CliSettings, ...] = (
        {"features": False},
        {"features": {"image_generation": True}},
        {},
    )

    assert same(written_settings(a_run(*levels))["features"], {"image_generation": True})


MOMENT = datetime(2026, 10, 28, 21, 30, 5, 250000)
EVERY_VALUE_TYPE: CliSettings = {
    "flag": True,
    "count": 3,
    "negative": -7,
    "ratio": 0.5,
    "integral_float": 2.0,
    "infinite": math.inf,
    "negative_infinite": -math.inf,
    "not_a_number": math.nan,
    "text": 'q"uote back\\slash\nnew\ttab del\x7f ünï',
    "day": date(2026, 10, 28),
    "clock": time(21, 30, 5),
    "offset_moment": MOMENT.replace(tzinfo=timezone(timedelta(hours=2))),
    "utc_moment": MOMENT.replace(tzinfo=UTC),
    "local_moment": MOMENT,
    "nested": [[1, 2], ["a"], []],
    "tables": [{"a": 1}, {"b": {"c": True}}],
    "empty": {},
    "a.b": 1,
    "": 2,
    "ü": 3,
    "inner": {"a.b": {"": [1.5]}},
}


def test_every_toml_value_type_is_written_so_that_reading_it_back_gives_the_same_value() -> None:
    expected = merged(DEFAULTS, EVERY_VALUE_TYPE)

    assert same(written_settings(a_run(EVERY_VALUE_TYPE)), expected)


class Plain:
    def __repr__(self) -> str:
        return VALUE_CANARY


@pytest.mark.parametrize("build", [a_run, a_grok_run], ids=["codex", "grok"])
@pytest.mark.parametrize(
    ("value", "type_name"),
    [
        (None, "NoneType"),
        (VALUE_CANARY.encode(), "bytes"),
        ({VALUE_CANARY}, "set"),
        (Plain(), "Plain"),
    ],
)
def test_a_value_toml_cannot_hold_is_refused_naming_its_path_and_type_but_not_its_content(
    build: Callable[..., CodexRun | GrokRun], value: object, type_name: str
) -> None:
    run = build({"features": {"kept": VALUE_CANARY, "broken": value}})  # type: ignore[dict-item]

    with pytest.raises(TypeError) as refused:
        run.run_files()

    assert type_name in str(refused.value)
    assert "features.broken" in str(refused.value)
    assert VALUE_CANARY not in str(refused.value)


def test_a_setting_value_is_in_the_file_only_and_no_token_is_in_argv_or_the_file(
    tmp_path: Path,
) -> None:
    run = a_run({"model_reasoning_summary": VALUE_CANARY})

    start = run.start(tmp_path / "home", tmp_path / "work")

    visible = " ".join([*start.child.arguments, *start.child.environment.values()])
    document = run.run_files()["config.toml"].decode()
    assert VALUE_CANARY in document
    assert VALUE_CANARY not in visible
    assert CANARY_ACCESS_TOKEN not in document + visible
    assert CANARY_ID_TOKEN not in document + visible
