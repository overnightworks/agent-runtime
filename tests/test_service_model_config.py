"""The model at three levels in the service configuration: defaults, app and agent."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from service_support import (
    LISTED_MODEL,
    TEST_ISSUER,
    UNLISTED_MODEL,
    _service_config_document,
    _toml_lines,
)

from agent_runtime_service.config import ConfigError, Provider, load_config

LISTED = f"{Provider.CLAUDE}/{LISTED_MODEL}"
UNLISTED = f"{Provider.CLAUDE}/{UNLISTED_MODEL}"
NO_TABLE_FOR_PROVIDER = f"{Provider.CODEX}/{LISTED_MODEL}"
MALFORMED = "Claude/Sonnet"

DEFAULTS = "defaults.model"
APP = "apps.songmaker.model"
AGENT = "apps.songmaker.agents.notes.model"


def _document() -> dict[str, Any]:
    return copy.deepcopy(_service_config_document(TEST_ISSUER, None, None))


def _set_default(document: dict[str, Any], name: str) -> None:
    document["defaults"]["model"] = name


def _set_app(document: dict[str, Any], name: str) -> None:
    document["apps"]["songmaker"]["model"] = name


def _set_agent(document: dict[str, Any], name: str) -> None:
    document["apps"]["songmaker"]["agents"] = {"notes": {"model": name}}


LEVELS: dict[str, tuple[Callable[[dict[str, Any], str], None], str]] = {
    "defaults": (_set_default, DEFAULTS),
    "app": (_set_app, APP),
    "agent": (_set_agent, AGENT),
}


def _load(tmp_path: Path, document: dict[str, Any]):
    path = tmp_path / "agent-runtime-service.toml"
    path.write_text("\n".join(_toml_lines(document)) + "\n", encoding="utf-8")
    return load_config(path)


def _refusal(tmp_path: Path, document: dict[str, Any]) -> str:
    with pytest.raises(ConfigError) as refused:
        _load(tmp_path, document)
    return str(refused.value)


@pytest.mark.parametrize("name", [MALFORMED, UNLISTED, NO_TABLE_FOR_PROVIDER])
@pytest.mark.parametrize("level", LEVELS)
def test_a_model_that_is_malformed_or_listed_by_no_cli_table_is_refused_naming_its_field(
    tmp_path, level, name
):
    setter, field = LEVELS[level]
    document = _document()
    setter(document, name)

    assert field in _refusal(tmp_path, document)


def test_a_file_that_sets_the_model_at_all_three_levels_loads(tmp_path):
    document = _document()
    for setter, _field in LEVELS.values():
        setter(document, LISTED)

    config = _load(tmp_path, document)

    assert config.defaults.model == LISTED
    assert config.apps["songmaker"].model == LISTED
    assert config.apps["songmaker"].agents["notes"].model == LISTED


def test_the_model_of_a_run_is_the_agents_else_the_apps_else_the_defaults(tmp_path):
    by_level = {level: f"{Provider.CLAUDE}/{level}" for level in LEVELS}
    document = _document()
    document["cli"]["claude"]["models"] = list(LEVELS)
    for level, (setter, _field) in LEVELS.items():
        setter(document, by_level[level])
    document["apps"]["songmaker"]["agents"]["other"] = {}
    config = _load(tmp_path, document)

    assert config.model_for("songmaker", "notes") == by_level["agent"]
    assert config.model_for("songmaker", "other") == by_level["app"]
    del document["apps"]["songmaker"]["model"]
    assert _load(tmp_path, document).model_for("songmaker", "other") == by_level["defaults"]
    assert config.model_for("presentator", "anything") == by_level["defaults"]


def test_a_file_without_defaults_is_refused_naming_defaults(tmp_path):
    document = _document()
    del document["defaults"]

    assert "defaults" in _refusal(tmp_path, document)


def test_defaults_without_a_model_is_refused_naming_the_field(tmp_path):
    document = _document()
    document["defaults"] = {}

    assert DEFAULTS in _refusal(tmp_path, document)


def test_an_unknown_key_in_defaults_is_refused_naming_it(tmp_path):
    document = _document()
    document["defaults"]["route"] = "api"

    assert "defaults.route" in _refusal(tmp_path, document)


def test_a_file_without_a_cli_table_is_refused_naming_the_default_model(tmp_path):
    document = _document()
    del document["cli"]

    assert DEFAULTS in _refusal(tmp_path, document)


def test_a_cli_table_that_lists_no_models_is_refused_naming_the_default_model(tmp_path):
    document = _document()
    document["cli"]["claude"]["models"] = []

    assert DEFAULTS in _refusal(tmp_path, document)
