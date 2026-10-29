"""``cli_settings``: the CLI's own settings at three levels, passed through unmapped."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from tests.support.codex_support import DEFAULT_SETTINGS
from tests.support.provider_test_support import a_codex_login
from tests.support.service_config_support import (
    VALID_CONFIG,
    assert_startup_refuses,
    written,
)

from agent_runtime.codex.run import CodexRun
from agent_runtime.codex_login import assess_codex_login
from agent_runtime_client.wire import TurnMessage
from agent_runtime_service.cli import EXIT_BAD_CONFIG, main
from agent_runtime_service.config import Provider, ServiceConfig, load_config

DEFAULTS_LINE = f'defaults = {{ model = "{Provider.ANTHROPIC}/sonnet" }}\n'
APP = "songmaker"
AGENT = "cowriter"
DOCUMENTED_APP = "notes"
DOCUMENTED_AGENT = "illustrator"
LEVELS = {
    "defaults": "defaults",
    "app": f"apps.{APP}",
    "agent": f"apps.{APP}.agents.{AGENT}",
}
VALUE_CANARY = "setting-value-canary-8c14"
SECOND_APP = '\n[apps.presentator]\nworkload_client = "presentator-test"\n'


def with_settings(**tables: str) -> str:
    """``VALID_CONFIG`` with an ``openai`` settings table body at the named levels."""
    text = VALID_CONFIG
    if "defaults" in tables:
        text = text.replace(
            DEFAULTS_LINE,
            f'[defaults]\nmodel = "{Provider.ANTHROPIC}/sonnet"\n'
            f"[defaults.cli_settings.openai]\n{tables['defaults']}\n",
        )
    for level in ("app", "agent"):
        if level in tables:
            text += f"\n[{LEVELS[level]}.cli_settings.openai]\n{tables[level]}\n"
    return text


def loaded(tmp_path: Path, text: str) -> ServiceConfig:
    return load_config(written(tmp_path, text))


def test_a_configuration_without_cli_settings_loads_with_three_empty_levels(tmp_path):
    config = loaded(tmp_path, VALID_CONFIG)

    assert config.cli_settings_levels(APP, AGENT, Provider.OPENAI) == ({}, {}, {})


def test_the_levels_come_defaults_first_then_the_app_then_the_agent_unmerged(tmp_path):
    config = loaded(
        tmp_path,
        with_settings(
            defaults='web_search = "live"\nfeatures = { plugins = true }',
            app="features = { hooks = true }",
            agent="features.image_generation = true",
        )
        + SECOND_APP,
    )

    assert (
        config.cli_settings_levels(APP, AGENT, Provider.OPENAI),
        config.cli_settings_levels(APP, "another", Provider.OPENAI),
        config.cli_settings_levels("presentator", AGENT, Provider.OPENAI),
    ) == (
        (
            {"web_search": "live", "features": {"plugins": True}},
            {"features": {"hooks": True}},
            {"features": {"image_generation": True}},
        ),
        (
            {"web_search": "live", "features": {"plugins": True}},
            {"features": {"hooks": True}},
            {},
        ),
        ({"web_search": "live", "features": {"plugins": True}}, {}, {}),
    )


OWNERS = {
    "model": "set model instead",
    "model_provider": "set model instead",
    "model_instructions_file": "set the run request's system text instead",
    "cli_auth_credentials_store": "set [cli.openai] token_file instead",
}


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("key", OWNERS)
def test_a_key_another_owner_holds_stops_startup_naming_its_level_its_key_and_its_owner(
    tmp_path, capsys, level, key
):
    text = with_settings(**{level: f'{key} = "{VALUE_CANARY}"'})

    assert main(["--config", str(written(tmp_path, text)), "--check"]) == EXIT_BAD_CONFIG

    refusal = capsys.readouterr().err
    assert f"{LEVELS[level]}.cli_settings.openai: {key} is the service's; {OWNERS[key]}" in refusal
    assert VALUE_CANARY not in refusal


def test_the_check_refuses_a_key_another_owner_holds_as_a_start_does(tmp_path, capsys):
    path = written(tmp_path, with_settings(app='model = "gpt-5.5"'))

    status = main(["--config", str(path), "--check"])

    assert (status, "model is the service's" in capsys.readouterr().err) == (EXIT_BAD_CONFIG, True)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        pytest.param("cli_settings.anthropic = {}", "cli_settings.anthropic", id="anthropic"),
        pytest.param("cli_settings.grok = {}", "cli_settings.grok", id="grok"),
        pytest.param('cli_settings.openai = "x"', "cli_settings.openai", id="not-a-table"),
    ],
)
def test_a_provider_key_the_service_does_not_serve_or_a_non_table_stops_startup_naming_it(
    tmp_path, capsys, body, field
):
    text = VALID_CONFIG + f"\n[{LEVELS['agent']}]\n{body}\n"

    assert_startup_refuses(tmp_path, capsys, text, field)


@pytest.mark.parametrize(
    "body",
    [
        "features.model = true",
        "invented_key = 1",
        'profile = "x"',
        'profiles = { x = { model = "y" } }',
        'projects = { "/work" = { trust_level = "trusted" } }',
    ],
)
def test_any_other_key_loads_because_typos_are_not_caught_at_startup(tmp_path, body):
    config = loaded(tmp_path, with_settings(agent=body))

    assert config.cli_settings_levels(APP, AGENT, Provider.OPENAI)[2] != {}


def test_a_quoted_dotted_key_stays_one_literal_key(tmp_path):
    config = loaded(tmp_path, with_settings(agent='"features.image_generation" = true'))

    assert config.cli_settings_levels(APP, AGENT, Provider.OPENAI)[2] == {
        "features.image_generation": True
    }


OPERATIONS_DOCUMENT = Path(__file__).parents[3] / "docs" / "operations.md"
NATIVE_EXAMPLE = (
    OPERATIONS_DOCUMENT.read_text(encoding="utf-8")
    .split("## CLI settings", 1)[1]
    .split("```toml\n", 1)[1]
    .split("```", 1)[0]
)
THE_EXAMPLES_APP = f'\n[apps.{DOCUMENTED_APP}]\nworkload_client = "{DOCUMENTED_APP}-test"\n'


def a_codex_run_of(config: ServiceConfig, app: str, agent: str) -> CodexRun:
    return CodexRun(
        Path("/opt/codex/bin/codex"),
        assess_codex_login(a_codex_login()),
        "system",
        (TurnMessage(role="user", text="question"),),
        model=None,
        settings=config.cli_settings_levels(app, agent, Provider.OPENAI),
    )


def test_the_documented_example_reaches_the_run_file_as_codex_reads_it(tmp_path):
    text = VALID_CONFIG.replace(
        DEFAULTS_LINE, f'[defaults]\nmodel = "{Provider.ANTHROPIC}/sonnet"\n'
    )
    config = loaded(tmp_path, text + THE_EXAMPLES_APP + NATIVE_EXAMPLE)

    run = a_codex_run_of(config, DOCUMENTED_APP, DOCUMENTED_AGENT)
    settings = tomllib.loads(run.run_files()["config.toml"].decode())

    assert settings == {
        **DEFAULT_SETTINGS,
        "web_search": "live",
        "features": {**DEFAULT_SETTINGS["features"], "image_generation": True},
    }


def test_the_quoted_variant_of_the_example_leaves_the_feature_off_and_adds_a_literal_key(tmp_path):
    quoted = NATIVE_EXAMPLE.replace("features.image_generation", '"features.image_generation"')
    text = VALID_CONFIG.replace(
        DEFAULTS_LINE, f'[defaults]\nmodel = "{Provider.ANTHROPIC}/sonnet"\n'
    )
    config = loaded(tmp_path, text + THE_EXAMPLES_APP + quoted)

    run = a_codex_run_of(config, DOCUMENTED_APP, DOCUMENTED_AGENT)
    settings = tomllib.loads(run.run_files()["config.toml"].decode())

    assert (settings["features"]["image_generation"], settings["features.image_generation"]) == (
        False,
        True,
    )
