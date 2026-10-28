"""The one left-to-right merge of the levels a CLI run's settings come from."""

from __future__ import annotations

import copy

import pytest

from agent_runtime.cli_settings import merged


def test_three_levels_with_disjoint_keys_merge_to_their_union():
    assert merged({"a": 1}, {"b": 2}, {"c": 3}) == {"a": 1, "b": 2, "c": 3}


@pytest.mark.parametrize(
    ("levels", "expected"), [((), {}), (({}, {}), {}), (({"a": 1}, {}), {"a": 1})]
)
def test_no_levels_and_empty_levels_merge_to_the_other_levels(levels, expected):
    assert merged(*levels) == expected


def test_a_merge_changes_none_of_its_inputs_and_shares_none_of_their_tables():
    lower = {"features": {"shell_tool": False}}
    upper = {"features": {"image_generation": True}}
    before = copy.deepcopy((lower, upper))

    result = merged(lower, upper)
    result["features"]["extra"] = 1

    assert (lower, upper) == before


def test_tables_merge_per_key_so_a_lower_key_the_upper_level_leaves_out_stays():
    lower = {"features": {"shell_tool": False, "image_generation": False}}
    upper = {"features": {"image_generation": True}}

    assert merged(lower, upper) == {"features": {"shell_tool": False, "image_generation": True}}


@pytest.mark.parametrize(
    ("lower", "upper", "expected"),
    [
        ({"a": [1, 2]}, {"a": [3]}, {"a": [3]}),
        ({"a": {"x": 1}}, {"a": False}, {"a": False}),
        ({"a": False}, {"a": {"x": 1}}, {"a": {"x": 1}}),
    ],
    ids=["array over array", "scalar over table", "table over scalar"],
)
def test_any_pair_that_is_not_two_tables_is_replaced_by_the_upper_value(lower, upper, expected):
    assert merged(lower, upper) == expected


def test_the_merge_is_one_left_to_right_pass_so_a_scalar_cuts_the_table_below_it():
    adapter = {"features": {"shell_tool": False}}
    defaults = {"features": False}
    app = {"features": {"image_generation": True}}

    assert merged(adapter, defaults, app) == {"features": {"image_generation": True}}
