"""The CLIs' own settings as the operator's config tables, one shape per CLI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Self, cast

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from agent_runtime.claude.run import check_tool_name, settings_document
from agent_runtime.cli_run import RELAY_SERVER_NAME
from agent_runtime.cli_settings import CliSettings, toml_key


class _CliSettingsSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


_ANY_ID: Final = "<id>"
_SET_MODEL_INSTEAD: Final = "set model instead"
_OwnedPaths = Mapping[tuple[str, ...], str]

_CODEX_SETTINGS_THE_SERVICE_OWNS: Final[_OwnedPaths] = {
    ("model",): _SET_MODEL_INSTEAD,
    ("model_provider",): _SET_MODEL_INSTEAD,
    ("model_instructions_file",): "set the run request's system text instead",
    ("cli_auth_credentials_store",): "set [cli.openai] token_file instead",
    ("mcp_servers", RELAY_SERVER_NAME): "the service attaches the run's tool relay",
}


_CLAUDE_SETTINGS_THE_SERVICE_OWNS: Final[_OwnedPaths] = {
    ("model",): _SET_MODEL_INSTEAD,
    ("fallbackModel",): _SET_MODEL_INSTEAD,
    ("agent",): "set the run request's system text and model instead",
    ("apiKeyHelper",): "set [cli.anthropic] token_file instead",
    ("env",): "the service builds the run's environment",
    ("permissions", "allow"): "set cli_settings.anthropic.tools instead",
}
_APP_TOOL_PREFIX: Final = "mcp__"

_GROK_LOGIN_FILE_OWNER: Final = "set [cli.xai] token_file instead"
_GROK_PATCH_OWNER: Final = "it can patch keys the service owns"
_GROK_SYSTEM_TEXT_OWNER: Final = "set the run request's system text instead"
_GROK_TOOL_FREE_OWNER: Final = "the run is tool-free (--{switch})"
_GROK_CREDENTIAL_CARRIER_OWNER: Final = (
    f"they can carry the inference credential; {_GROK_LOGIN_FILE_OWNER}"
)
# Grok reads the sign-in table under both of its names, so each owned path is written once.
_GROK_SIGN_IN_TABLES: Final = ("auth", "grok_com_config")
_GROK_SIGN_IN_KEYS: Final = (
    "auth_provider_command",
    "preferred_method",
    "oidc",
    "oauth2",
    "token_header",
)


def _grok_settings_the_service_owns() -> _OwnedPaths:
    owned: dict[tuple[str, ...], str] = {
        ("models", "default"): _SET_MODEL_INSTEAD,
        ("permission", "deny"): _GROK_TOOL_FREE_OWNER.format(switch='deny "*"'),
        ("subagents", "enabled"): _GROK_TOOL_FREE_OWNER.format(switch="no-subagents"),
        ("disable_web_search",): _GROK_TOOL_FREE_OWNER.format(switch="disable-web-search"),
        ("models", "extra_headers"): _GROK_CREDENTIAL_CARRIER_OWNER,
        ("models", "agent_type"): _GROK_SYSTEM_TEXT_OWNER,
        ("model", _ANY_ID, "model_provider"): _SET_MODEL_INSTEAD,
        ("model", _ANY_ID, "system_prompt_label"): _GROK_SYSTEM_TEXT_OWNER,
        ("model", _ANY_ID, "agent_type"): _GROK_SYSTEM_TEXT_OWNER,
        ("agent",): _GROK_SYSTEM_TEXT_OWNER,
        ("model_providers",): _GROK_LOGIN_FILE_OWNER,
        ("auth_provider",): _GROK_LOGIN_FILE_OWNER,
    }
    for table in _GROK_SIGN_IN_TABLES:
        for key in _GROK_SIGN_IN_KEYS:
            owned[(table, key)] = _GROK_LOGIN_FILE_OWNER
    for key in ("api_key", "env_key", "auth_provider"):
        owned[("model", _ANY_ID, key)] = _GROK_LOGIN_FILE_OWNER
    for key in ("extra_headers", "query_params", "mtls_cert_dir"):
        owned[("model", _ANY_ID, key)] = _GROK_CREDENTIAL_CARRIER_OWNER
    for path in (
        ("experiments",),
        ("version_overrides",),
        ("campaigns",),
        ("features", "managed_config"),
        ("features", "campaigns"),
        ("endpoints", "managed_config_url"),
    ):
        owned[path] = _GROK_PATCH_OWNER
    return owned


_SETTINGS_THE_SERVICE_OWNS_BY_TABLE: Final[Mapping[str, _OwnedPaths]] = {
    "openai": _CODEX_SETTINGS_THE_SERVICE_OWNS,
    "xai": _grok_settings_the_service_owns(),
}


def _set_path(settings: object, path: tuple[str, ...]) -> tuple[str, ...] | None:
    """The concrete path of ``path`` in the parsed table tree, or ``None`` when it is not set.

    An ``<id>`` segment matches any key; a value, even an empty table or array, makes a path set.
    The walk never descends into an array.
    """
    if not isinstance(settings, dict):
        return None
    key, *rest = path
    for found in settings if key == _ANY_ID else [key]:
        if found not in settings:
            continue
        if not rest:
            return (found,)
        below = _set_path(settings[found], tuple(rest))
        if below is not None:
            return (found, *below)
    return None


def _first_owned_refusal(settings: object, owned: _OwnedPaths, prefix: str = "") -> str | None:
    for path, owner in owned.items():
        found = _set_path(settings, path)
        if found is not None:
            path_text = ".".join(toml_key(key) for key in found)
            return f"{prefix}{path_text} is the service's; {owner}"
    return None


class ClaudeCliSettingsConfig(_CliSettingsSection):
    """Claude's ``tools`` and ``settings`` as one level of the operator's table.

    A field a level does not set stays absent from that level, so it never replaces a lower
    one; ``tools = []`` is set and switches a lower list off.
    """

    tools: list[str] | None = None
    settings: dict[str, object] | None = None

    @field_validator("tools")
    @classmethod
    def _tool_names_the_cli_can_take_one_by_one(cls, tools: list[str] | None) -> list[str] | None:
        for index, name in enumerate(tools or []):
            try:
                check_tool_name(name)
            except ValueError as refusal:
                raise ValueError(f"tools[{index}]: {refusal}") from None
        return tools

    @field_validator("settings")
    @classmethod
    def _values_json_can_hold(cls, settings: dict[str, object] | None) -> dict[str, object] | None:
        try:
            settings_document(cast("CliSettings", settings or {}))
        except TypeError as refusal:
            raise ValueError(str(refusal)) from None
        return settings

    @model_validator(mode="after")
    def _keys_the_service_owns_are_not_set_here(self) -> Self:
        for index, name in enumerate(self.tools or []):
            if name.startswith(_APP_TOOL_PREFIX):
                raise ValueError(
                    f"tools[{index}] is the service's; set the app's MCP origin instead"
                )
        refusal = _first_owned_refusal(
            self.settings or {}, _CLAUDE_SETTINGS_THE_SERVICE_OWNS, prefix="settings."
        )
        if refusal is not None:
            raise ValueError(refusal)
        return self

    def as_level(self) -> CliSettings:
        """The fields this level sets, as the run adapter's level."""
        level: CliSettings = {}
        if self.tools is not None:
            level["tools"] = list(self.tools)
        if self.settings is not None:
            level["settings"] = cast("CliSettings", self.settings)
        return level


class CliSettingsConfig(_CliSettingsSection):
    """The CLIs' own settings in each CLI's own shape, passed through without a mapping.

    Only a key whose fact another owner already holds is refused, naming that owner.
    """

    anthropic: ClaudeCliSettingsConfig = ClaudeCliSettingsConfig()
    openai: dict[str, object] = {}

    xai: dict[str, object] = {}

    @field_validator("openai", "xai")
    @classmethod
    def _keys_the_service_owns_are_not_set_here(
        cls, settings: dict[str, object], info: ValidationInfo
    ) -> dict[str, object]:
        assert info.field_name is not None  # noqa: S101 - a field validator always runs for a named field
        refusal = _first_owned_refusal(
            settings, _SETTINGS_THE_SERVICE_OWNS_BY_TABLE[info.field_name]
        )
        if refusal is not None:
            raise ValueError(refusal)
        return settings
