"""``cli_settings``: the CLI's own settings at three levels, passed through unmapped."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from tests.support.codex_support import DEFAULT_SETTINGS
from tests.support.provider_test_support import a_codex_login, a_grok_login, exception_chain
from tests.support.service_config_support import (
    VALID_CONFIG,
    assert_startup_refuses,
    written,
)

from agent_runtime.codex.run import CodexRun
from agent_runtime.codex_login import assess_codex_login
from agent_runtime.grok.run import GrokRun
from agent_runtime.grok_login import assess_grok_login
from agent_runtime_client.wire import TurnMessage
from agent_runtime_service.cli import EXIT_BAD_CONFIG, main
from agent_runtime_service.config import ConfigError, Provider, ServiceConfig, load_config

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


def test_xais_levels_hold_only_xais_settings_while_codex_and_claude_are_set_at_every_level(
    tmp_path,
):
    text = with_settings(
        provider="xai",
        defaults='ui.theme = "a"',
        app='ui.theme = "b"',
        agent='ui.theme = "c"',
    )
    for level in LEVELS.values():
        text += f'\n[{level}.cli_settings.anthropic]\ntools = ["Read"]\n'
        text += f'\n[{level}.cli_settings.openai]\nweb_search = "live"\n'

    config = loaded(tmp_path, text)

    assert config.cli_settings_levels(APP, AGENT, Provider.XAI) == (
        {"ui": {"theme": "a"}},
        {"ui": {"theme": "b"}},
        {"ui": {"theme": "c"}},
    )
    assert config.cli_settings_levels(APP, AGENT, Provider.OPENAI) == ({"web_search": "live"},) * 3


TOKEN_OWNER = "set [cli.xai] token_file instead"
SYSTEM_TEXT_OWNER = "set the run request's system text instead"
PATCH_OWNER = "it can patch keys the service owns"
CARRIER_OWNER = f"they can carry the inference credential; {TOKEN_OWNER}"
MODEL_OWNER = "set model instead"
WHOLE_TABLE_OWNERS = (
    ("agent", SYSTEM_TEXT_OWNER),
    ("model_providers", TOKEN_OWNER),
    ("auth_provider", TOKEN_OWNER),
)


TABLE_HERE = "@table@"


def tool_free(switch: str) -> str:
    return f"the run is tool-free ({switch})"


def owned(provider: str, body: str, path: str, owner: str):
    return pytest.param(provider, body, path, owner, id=f"{provider}-{path}-{body[:12]}")


def xai_rows(*rows: tuple[str, str, str]):
    return (owned("xai", body, path, owner) for body, path, owner in rows)


OWNED_PATHS = [
    owned("openai", f'model = "{VALUE_CANARY}"', "model", MODEL_OWNER),
    owned("openai", f'model_provider = "{VALUE_CANARY}"', "model_provider", MODEL_OWNER),
    owned(
        "openai",
        f'model_instructions_file = "{VALUE_CANARY}"',
        "model_instructions_file",
        SYSTEM_TEXT_OWNER,
    ),
    owned(
        "openai",
        f'cli_auth_credentials_store = "{VALUE_CANARY}"',
        "cli_auth_credentials_store",
        "set [cli.openai] token_file instead",
    ),
    *xai_rows(
        (f'models.default = "{VALUE_CANARY}"', "models.default", MODEL_OWNER),
        (f'permission.deny = ["{VALUE_CANARY}"]', "permission.deny", tool_free('--deny "*"')),
        (
            f'subagents.enabled = "{VALUE_CANARY}"',
            "subagents.enabled",
            tool_free("--no-subagents"),
        ),
        (
            f'disable_web_search = "{VALUE_CANARY}"',
            "disable_web_search",
            tool_free("--disable-web-search"),
        ),
        (f'auth = {{ oidc = {{ a = "{VALUE_CANARY}" }} }}', "auth.oidc", TOKEN_OWNER),
        (
            f'models.extra_headers = {{ x = "{VALUE_CANARY}" }}',
            "models.extra_headers",
            CARRIER_OWNER,
        ),
        ("models.extra_headers = {}", "models.extra_headers", CARRIER_OWNER),
        (f'model.m1.model_provider = "{VALUE_CANARY}"', "model.m1.model_provider", MODEL_OWNER),
        (
            f'model.m1.system_prompt_label = "{VALUE_CANARY}"',
            "model.m1.system_prompt_label",
            SYSTEM_TEXT_OWNER,
        ),
        (f'models.agent_type = "{VALUE_CANARY}"', "models.agent_type", SYSTEM_TEXT_OWNER),
        (f'model.m1.agent_type = "{VALUE_CANARY}"', "model.m1.agent_type", SYSTEM_TEXT_OWNER),
        (f'campaigns = [{{ id = "{VALUE_CANARY}" }}]', "campaigns", PATCH_OWNER),
        (
            f'features.managed_config = "{VALUE_CANARY}"',
            "features.managed_config",
            PATCH_OWNER,
        ),
        (f'features.campaigns = "{VALUE_CANARY}"', "features.campaigns", PATCH_OWNER),
        (
            f'endpoints.managed_config_url = "{VALUE_CANARY}"',
            "endpoints.managed_config_url",
            PATCH_OWNER,
        ),
    ),
    *(
        owned("xai", f'{table}.{key} = "{VALUE_CANARY}"', f"{table}.{key}", TOKEN_OWNER)
        for table in ("auth", "grok_com_config")
        for key in ("auth_provider_command", "preferred_method", "oidc", "oauth2", "token_header")
    ),
    *(
        owned(
            "xai", f'model.m1.{key} = {{ x = "{VALUE_CANARY}" }}', f"model.m1.{key}", CARRIER_OWNER
        )
        for key in ("extra_headers", "query_params", "mtls_cert_dir")
    ),
    *(
        owned("xai", f"model.m1.{key} = {{}}", f"model.m1.{key}", CARRIER_OWNER)
        for key in ("extra_headers", "query_params", "mtls_cert_dir")
    ),
    *(
        owned(
            "xai",
            f'model."grok-x.1".{key} = "{VALUE_CANARY}"',
            f'model."grok-x.1".{key}',
            TOKEN_OWNER,
        )
        for key in ("api_key", "env_key", "auth_provider")
    ),
    *(
        owned(
            "xai",
            f'model.{quoted}.api_key = "{VALUE_CANARY}"',
            f"model.{shown}.api_key",
            TOKEN_OWNER,
        )
        for quoted, shown in (
            ("'a\"b.c'", '"a\\"b.c"'),
            ('""', '""'),
            ('"my id"', '"my id"'),
        )
    ),
    owned(
        "xai",
        f'model."grok-x.1".system_prompt_label = "{VALUE_CANARY}"',
        'model."grok-x.1".system_prompt_label',
        SYSTEM_TEXT_OWNER,
    ),
    owned(
        "xai",
        f'[[{TABLE_HERE}.version_overrides]]\nid = "{VALUE_CANARY}"',
        "version_overrides",
        PATCH_OWNER,
    ),
    *(
        owned("xai", f'{key} = {{ x = "{VALUE_CANARY}" }}', key, owner)
        for key, owner in WHOLE_TABLE_OWNERS
    ),
    *(owned("xai", f"{key} = {{}}", key, owner) for key, owner in WHOLE_TABLE_OWNERS),
    *(
        owned("xai", f"{key} = []", key, PATCH_OWNER)
        for key in ("experiments", "version_overrides", "campaigns")
    ),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize(("provider", "body", "path", "owner"), OWNED_PATHS)
def test_a_path_another_owner_holds_stops_startup_naming_its_level_its_path_and_its_owner(
    tmp_path, capsys, level, provider, body, path, owner
):
    table = f"{LEVELS[level]}.cli_settings.{provider}"
    config_file = written(
        tmp_path, with_settings(provider=provider, **{level: body.replace(TABLE_HERE, table)})
    )

    assert main(["--config", str(config_file), "--check"]) == EXIT_BAD_CONFIG
    with pytest.raises(ConfigError) as refused:
        load_config(config_file)

    refusal = capsys.readouterr().err
    assert f"{table}: {path} is the service's; {owner}" in refusal
    assert VALUE_CANARY not in refusal
    assert exception_chain(refused.value) == [refused.value]
    assert VALUE_CANARY not in repr(refused.value.args)


def test_an_owned_path_stops_a_start_as_it_stops_the_check(tmp_path, capsys):
    path = written(tmp_path, with_settings(provider="xai", app='models.default = "x"'))

    assert main(["--config", str(path)]) == EXIT_BAD_CONFIG

    refusal = capsys.readouterr().err
    assert f"{LEVELS['app']}.cli_settings.xai: models.default is the service's" in refusal


PASS_THROUGH = {
    "egress": 'endpoints.deployment_key = "k"\nmodel.m1.base_url = "https://h.test"\n'
    'model.m1.env_http_headers = { x = "ENV_NAME" }',
    "model choices": 'models.allowed_models = ["a"]\nmodels.web_search = "m"\n'
    'models.default_reasoning_effort = "low"',
    "login labels": "auth.auth_token_ttl = 60\nauth.force_login_team_uuid = []\n"
    "grok_com_config.auth_token_ttl = 60\ngrok_com_config.force_login_team_uuid = []",
    "rules": 'paths.extra_rule_dirs = ["r"]\ncompat.x.rules = true',
    "hooks and display": 'hooks.x = []\nui.theme = "dark"',
    "tool surface": 'permission.allow = ["x"]\nsubagents.models.x = "m"\n'
    'mcp_servers.x.command = "c"\ntools.x = 1\nsandbox.x = 1\nauto_mode.x = 1\n'
    "shell_environment_policy.x = 1\nskills.x = 1",
    "the rest": "features.image_gen = true\nmemory = true\ncli.x = 1",
    "arrays of tables": "mcp_servers.x.args = [{ a = 1 }]\nhooks.y = [{ b = [{ c = 2 }] }]",
    "unknown keys": 'invented_key = 1\nnested.invented.deep = "x"',
    "quoted dotted keys": '"auth.preferred_method" = "x"\n"models.default" = "y"',
}


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("body", PASS_THROUGH.values(), ids=PASS_THROUGH)
def test_every_pass_through_class_loads_and_is_returned_at_its_level_as_written(
    tmp_path, level, body
):
    config = loaded(tmp_path, with_settings(provider="xai", **{level: body}))

    levels = config.cli_settings_levels(APP, AGENT, Provider.XAI)

    assert levels[list(LEVELS).index(level)] == tomllib.loads(body)
    assert tomllib.loads(a_grok_run_of(levels).run_files()["config.toml"].decode()) == (
        tomllib.loads(body)
    )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        pytest.param("cli_settings.grok = {}", "cli_settings.grok", id="grok"),
        pytest.param('cli_settings.openai = "x"', "cli_settings.openai", id="not-a-table"),
        pytest.param('cli_settings.xai = "x"', "cli_settings.xai", id="xai-not-a-table"),
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
GROK_EXAMPLE = (
    OPERATIONS_DOCUMENT.read_text(encoding="utf-8")
    .split("### Grok", 1)[1]
    .split("```toml\n", 1)[1]
    .split("```", 1)[0]
)
THE_EXAMPLES_APP = f'\n[apps.{DOCUMENTED_APP}]\nworkload_client = "{DOCUMENTED_APP}-test"\n'


def a_grok_run_of(levels: tuple[dict[str, object], ...]) -> GrokRun:
    return GrokRun(
        Path("/opt/grok/bin/grok"),
        assess_grok_login(json.dumps(a_grok_login()).encode()),
        "system",
        (TurnMessage(role="user", text="question"),),
        model=None,
        settings=levels,
    )


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


def test_the_documented_grok_example_reaches_a_grok_runs_config_file_as_written(tmp_path):
    text = VALID_CONFIG.replace(
        DEFAULTS_LINE, f'[defaults]\nmodel = "{Provider.ANTHROPIC}/sonnet"\n'
    )
    config = loaded(tmp_path, text + THE_EXAMPLES_APP + GROK_EXAMPLE)

    run = a_grok_run_of(config.cli_settings_levels(DOCUMENTED_APP, "any-agent", Provider.XAI))

    assert tomllib.loads(run.run_files()["config.toml"].decode()) == (
        tomllib.loads(GROK_EXAMPLE)["apps"][DOCUMENTED_APP]["cli_settings"]["xai"]
    )
