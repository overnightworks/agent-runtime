"""The CLIs' own settings as the operator's config tables, one shape per CLI."""

from __future__ import annotations

from typing import Final, Self, cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agent_runtime.claude.run import check_tool_name, settings_document
from agent_runtime.cli_settings import CliSettings


class _CliSettingsSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


_CODEX_KEYS_THE_SERVICE_OWNS: Final = {
    "model": "model",
    "model_provider": "model",
    "model_instructions_file": "the run request's system text",
    "cli_auth_credentials_store": "[cli.openai] token_file",
}


_CLAUDE_SETTINGS_THE_SERVICE_OWNS: Final = {
    ("model",): "set model instead",
    ("fallbackModel",): "set model instead",
    ("agent",): "set the run request's system text and model instead",
    ("apiKeyHelper",): "set [cli.anthropic] token_file instead",
    ("env",): "the service builds the run's environment",
    ("permissions", "allow"): "set cli_settings.anthropic.tools instead",
}
_APP_TOOL_PREFIX: Final = "mcp__"


def _claude_setting_is_set(settings: object, path: tuple[str, ...]) -> bool:
    for key in path:
        if not isinstance(settings, dict) or key not in settings:
            return False
        settings = settings[key]
    return True


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
        for path, owner in _CLAUDE_SETTINGS_THE_SERVICE_OWNS.items():
            if _claude_setting_is_set(self.settings or {}, path):
                raise ValueError(f"settings.{'.'.join(path)} is the service's; {owner}")
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

    @field_validator("openai")
    @classmethod
    def _keys_the_service_owns_are_not_set_here(
        cls, settings: dict[str, object]
    ) -> dict[str, object]:
        for key, owner in _CODEX_KEYS_THE_SERVICE_OWNS.items():
            if key in settings:
                raise ValueError(f"{key} is the service's; set {owner} instead")
        return settings
