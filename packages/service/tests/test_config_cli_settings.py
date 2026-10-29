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


def with_settings(*, provider: str = "openai", **tables: str) -> str:
    """``VALID_CONFIG`` with a ``provider`` settings table body at the named levels."""
    text = VALID_CONFIG
    if "defaults" in tables:
        text = text.replace(
            DEFAULTS_LINE,
            f'[defaults]\nmodel = "{Provider.ANTHROPIC}/sonnet"\n'
            f"[defaults.cli_settings.{provider}]\n{tables['defaults']}\n",
        )
    for level in ("app", "agent"):
        if level in tables:
            text += f"\n[{LEVELS[level]}.cli_settings.{provider}]\n{tables[level]}\n"
    return text


def loaded(tmp_path: Path, text: str) -> ServiceConfig:
    return load_config(written(tmp_path, text))


@pytest.mark.parametrize("provider", list(Provider))
def test_a_configuration_without_cli_settings_loads_with_three_empty_levels(tmp_path, provider):
    config = loaded(tmp_path, VALID_CONFIG)

    assert config.cli_settings_levels(APP, AGENT, provider) == ({}, {}, {})


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


def claude_levels(tmp_path, text, agent=AGENT, app=APP):
    return loaded(tmp_path, text).cli_settings_levels(app, agent, Provider.ANTHROPIC)


def test_claude_levels_carry_only_the_fields_a_level_sets_lowest_level_first(tmp_path):
    text = with_settings(
        provider="anthropic",
        defaults='tools = ["Bash"]\nsettings = { outputStyle = "a" }',
        app='tools = ["Bash", "WebSearch"]',
        agent="tools = []",
    )

    assert (
        claude_levels(tmp_path, text),
        claude_levels(tmp_path, text, agent="another"),
    ) == (
        (
            {"tools": ["Bash"], "settings": {"outputStyle": "a"}},
            {"tools": ["Bash", "WebSearch"]},
            {"tools": []},
        ),
        (
            {"tools": ["Bash"], "settings": {"outputStyle": "a"}},
            {"tools": ["Bash", "WebSearch"]},
            {},
        ),
    )


CLAUDE_OWNERS = {
    'tools = ["mcp__relay__x"]': ("tools[0]", "set the app's MCP origin instead"),
    f'settings.model = "{VALUE_CANARY}"': ("settings.model", "set model instead"),
    f'settings.fallbackModel = "{VALUE_CANARY}"': ("settings.fallbackModel", "set model instead"),
    f'settings.agent = "{VALUE_CANARY}"': (
        "settings.agent",
        "set the run request's system text and model instead",
    ),
    f'settings.apiKeyHelper = "{VALUE_CANARY}"': (
        "settings.apiKeyHelper",
        "set [cli.anthropic] token_file instead",
    ),
    f'settings.env = {{ A = "{VALUE_CANARY}" }}': (
        "settings.env",
        "the service builds the run's environment",
    ),
    f'settings.permissions.allow = ["{VALUE_CANARY}"]': (
        "settings.permissions.allow",
        "set cli_settings.anthropic.tools instead",
    ),
}


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("body", CLAUDE_OWNERS)
def test_a_claude_key_another_owner_holds_stops_startup_naming_level_key_and_owner(
    tmp_path, capsys, level, body
):
    key, owner = CLAUDE_OWNERS[body]
    text = with_settings(provider="anthropic", **{level: body})

    assert main(["--config", str(written(tmp_path, text)), "--check"]) == EXIT_BAD_CONFIG

    refusal = capsys.readouterr().err
    assert f"{LEVELS[level]}.cli_settings.anthropic: {key} is the service's; {owner}" in refusal
    assert VALUE_CANARY not in refusal


@pytest.mark.parametrize(
    "body",
    [
        'settings.permissions.deny = ["Read"]',
        'settings.permissions.defaultMode = "plan"',
        'settings.permissions.ask = ["Bash"]',
        'settings.hooks.PreToolUse = []',
        'settings.outputStyle = "Explanatory"',
        'settings.forceLoginMethod = "claudeai"',
        'settings.modelOverrides = { a = "b" }',
        "settings.sandbox.enabled = false",
        "settings.inventedK2b = 1",
        'tools = ["InventedTool"]',
    ],
)
def test_any_other_claude_key_or_tool_loads_because_typos_are_not_caught(tmp_path, body):
    levels = claude_levels(tmp_path, with_settings(provider="anthropic", agent=body))

    assert levels[2] != {}


UNENCODABLE_BODIES = {
    'tools = [""]': "cli_settings.anthropic.tools",
    f'tools = ["Bash,{VALUE_CANARY}"]': "cli_settings.anthropic.tools",
    f'tools = ["Bash {VALUE_CANARY}"]': "cli_settings.anthropic.tools",
    f'tools = ["-{VALUE_CANARY}"]': "cli_settings.anthropic.tools",
    'tools = "Bash"': "cli_settings.anthropic.tools",
    "settings = 1": "cli_settings.anthropic.settings",
    "extra = 1": "cli_settings.anthropic.extra",
    "settings.x = 1979-05-27": "settings.x is a date",
    "settings.x = 1979-05-27T07:32:00Z": "settings.x is a datetime",
    "settings.x = 07:32:00": "settings.x is a time",
    "settings.x = inf": "settings.x is a float",
    "settings.x = -inf": "settings.x is a float",
    "settings.x = nan": "settings.x is a float",
    "settings.y.x = [{ z = 1979-05-27 }]": "settings.y.x[0].z is a date",
}


@pytest.mark.parametrize("body", UNENCODABLE_BODIES)
def test_what_claude_cannot_be_given_faithfully_stops_startup_naming_the_path(
    tmp_path, capsys, body
):
    canary = f'\nsettings.canary = "{VALUE_CANARY}"' if body.startswith("settings.") else ""
    text = with_settings(provider="anthropic", agent=body + canary)

    assert main(["--config", str(written(tmp_path, text)), "--check"]) == EXIT_BAD_CONFIG

    refusal = capsys.readouterr().err
    assert UNENCODABLE_BODIES[body] in refusal
    assert VALUE_CANARY not in refusal


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
