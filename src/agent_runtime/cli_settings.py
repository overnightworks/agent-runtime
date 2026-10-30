"""The settings of one CLI run, merged from its levels in one left-to-right pass.

A run's settings come from ordered levels, lowest first: the adapter's defaults, then the
operator's `[defaults]`, app and agent tables (#488 F1). Tables merge per key, recursively, and
any other pair is replaced by the later value. The merge is not associative, so callers pass
every level to one call instead of merging some levels first.

The merged settings are written as a TOML document (`toml_document`), the form the Codex and
Grok CLIs read their configuration in.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time
from typing import Final

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


def toml_basic_string(value: str) -> str:
    # JSON string escapes are a subset of TOML basic string escapes, except that TOML also
    # forbids the raw DEL character, which JSON leaves as it is.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


_BARE_KEY: Final = re.compile(r"[A-Za-z0-9_-]+")


def toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else toml_basic_string(key)


def _toml_float(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)


def _toml_value(path: str, value: TomlValue) -> str:
    # `bool` is checked before its base class `int`.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _toml_float(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return toml_basic_string(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, list):
        items = (_toml_value(f"{path}[{index}]", item) for index, item in enumerate(value))
        return f"[{', '.join(items)}]"
    if isinstance(value, dict):
        return _toml_inline_table(path, value)
    raise TypeError(f"Setting {path} is a {type(value).__name__}, which TOML cannot hold")


def _toml_inline_table(path: str, table: CliSettings) -> str:
    entries = (
        f"{toml_key(key)} = {_toml_value(f'{path}.{key}' if path else key, value)}"
        for key, value in table.items()
    )
    return f"{{ {', '.join(entries)} }}" if table else "{}"


def toml_document(settings: CliSettings) -> bytes:
    lines = (
        f"{toml_key(key)} = {_toml_value(key, value)}\n" for key, value in settings.items()
    )
    return "".join(lines).encode()
