"""The settings of one CLI run, merged from its levels in one left-to-right pass.

A run's settings come from ordered levels, lowest first: the adapter's defaults, then the
operator's `[defaults]`, app and agent tables (#488 F1). Tables merge per key, recursively, and
any other pair is replaced by the later value. The merge is not associative, so callers pass
every level to one call instead of merging some levels first.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time

type TomlValue = (
    bool | int | float | str | date | time | datetime | list[TomlValue] | CliSettings
)
type CliSettings = dict[str, TomlValue]


def merged(*levels: Mapping[str, TomlValue]) -> CliSettings:
    """The levels laid over one another, later levels winning; a new table, nothing shared."""
    result: CliSettings = {}
    for level in levels:
        for key, value in level.items():
            lower = result.get(key)
            if isinstance(lower, dict) and isinstance(value, dict):
                result[key] = merged(lower, value)
            else:
                result[key] = _copied(value)
    return result


def _copied(value: TomlValue) -> TomlValue:
    if isinstance(value, dict):
        return merged(value)
    if isinstance(value, list):
        return [_copied(item) for item in value]
    return value
