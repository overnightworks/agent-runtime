"""The service's one configuration: identity, listener, the model, the programs, the API and apps.

The configuration names the model of every run at three levels: ``[defaults] model`` is
required, and ``model`` may be set again in ``[apps.<app>]`` and in
``[apps.<app>.agents.<agent>]``. Each name is ``<provider>/<model>`` in the wire's grammar, and
startup refuses one that no ``[cli.<provider>]`` table lists in its ``models``.

``cli_settings`` may be set at the same three levels. ``cli_settings.openai`` holds Codex's own
settings in Codex's own shape; ``cli_settings.xai`` holds Grok's, in Grok's own ``config.toml``
keys; ``cli_settings.anthropic`` holds Claude's, a ``tools`` list of built-in tool names and a
``settings`` table in Claude's own settings keys. The service passes
them through without a mapping, lowest level first, and refuses only a key whose fact another
owner holds, and a value its run files cannot encode faithfully. It governs CLI runs only.

The service runs the subscription programs of Claude (``[cli.anthropic]``), of Codex
(``[cli.openai]``) and of Grok (``[cli.xai]``). Each such table names the absolute program, the
exact version it must report, the models it serves and its token file (a Claude setup token, a
Codex sign-in document, or a Grok sign-in document), beside the ``[cli]`` section's private
``working_root``, checked at load.
``identity.api_groups`` and ``[api]`` come together. An
app that offers tools names its one https MCP origin, and the service signs a capability for
each run that grants the tools its request names; an origin is never shared between apps and
needs the signing key. The ``[mcp]`` section names the one CA bundle (``ca_file``, an absolute
path) the service trusts for those origins; it is required as soon as any app declares an
origin, and otherwise optional and unused.

A TOML file is read once at startup into frozen models that forbid unknown
fields. A file the models refuse stops the service with an error naming the
field, so a typo never becomes a silently ignored policy. The signing key file
and each table's token file the TOML names are read and checked with it, in the same pass;
the configuration keeps the key, but only a token file's path, since the runner reads the
token again at the start of every run.
"""

from __future__ import annotations

import ipaddress
import re
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Final, Protocol, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    PrivateAttr,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from webauth.https_client import is_https_url
from webauth.oidc_login import AdmissionConfig

from agent_runtime.cli_settings import CliSettings
from agent_runtime.codex_login import CodexLoginVerdict, assess_codex_login
from agent_runtime.config import PrivateWorkingRoot, WorkingRootNotPrivateError
from agent_runtime.grok_login import GrokLoginVerdict, assess_grok_login
from agent_runtime.secret_file import (
    UnsoundSecretFile,
    read_secret_file,
    read_token_file,
    shown_path,
    shown_text,
)
from agent_runtime_client.capability import McpOrigin
from agent_runtime_client.wire import (
    MAX_EVENT_BYTES,
    MAX_RUN_FILES_BYTES,
    MODEL_PART_PATTERN,
    AgentName,
    ModelName,
)
from agent_runtime_service.api_credentials import read_api_credentials
from agent_runtime_service.capability import CapabilityScope
from agent_runtime_service.cli_settings_config import CliSettingsConfig
from agent_runtime_service.signing import SigningKey, load_signing_key

NonEmptyText = Annotated[str, Field(min_length=1, strict=True)]


def _https_url_the_verifier_accepts(url: str) -> str:
    """Stops startup as a field error on the key-set URL webauth's verifier would refuse."""
    if not is_https_url(url):
        raise ValueError(
            "must be an https URL naming a host, a valid port if any,"
            " and no credentials, spaces or control characters"
        )
    return url


HttpsUrl = Annotated[str, AfterValidator(_https_url_the_verifier_accepts)]


APP_GROUP_PREFIX: Final = "/apps/"


class ConfigError(ValueError):
    """A configuration file the service refuses to start with; the message names the field."""


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    XAI = "xai"


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _group_path(name: str) -> str:
    if not name.startswith("/"):
        raise ValueError("must begin with /")
    return name


GroupName = Annotated[NonEmptyText, AfterValidator(_group_path)]


class IdentityConfig(_Section):
    """Who issues the tokens the service accepts, and which claims carry the groups."""

    issuer: NonEmptyText
    jwks_url: HttpsUrl
    audience: NonEmptyText
    subscription_groups: frozenset[GroupName] = Field(min_length=1)
    api_groups: frozenset[GroupName] = frozenset()
    group_claim: NonEmptyText
    leeway_seconds: int = Field(default=0, ge=0, le=300)


def _not_a_path_to(file_description: str) -> ValueError:
    return ValueError(f"must be an absolute path to the {file_description}; ~ is not expanded")


def _absolute_path_to(file_description: str, value: object) -> Path:
    if not isinstance(value, str):
        raise _not_a_path_to(file_description)
    if not Path(value).is_absolute():
        raise ValueError(
            f"{shown_text(value)} must be an absolute path to the {file_description};"
            " ~ is not expanded"
        )
    return Path(value)


def _signing_key_in_file(key_file: object) -> SigningKey:
    """Loads the key the absolute path names; a refusal stops startup as a field error."""
    return load_signing_key(_absolute_path_to("signing key file", key_file))


CODEX_LOGIN_REFUSALS: Final = {
    CodexLoginVerdict.MALFORMED: "is not a Codex login document",
    CodexLoginVerdict.INCOMPLETE: (
        "is not a complete Codex sign-in; an API key (OPENAI_API_KEY) is not a subscription login"
    ),
}

GROK_LOGIN_REFUSALS: Final = {
    GrokLoginVerdict.MALFORMED: "is not a Grok login document",
    GrokLoginVerdict.INCOMPLETE: (
        "is not a complete Grok sign-in; an API key (XAI_API_KEY) is not a subscription login"
    ),
}


class _Judged(Protocol):
    @property
    def verdict(self) -> StrEnum: ...


def _login_reader[Login: _Judged](
    assess: Callable[[bytes], Login], usable: StrEnum, refusals: Mapping[StrEnum, str]
) -> Callable[[Path], Login]:
    """A reader of one login file: the usable login it holds; any other verdict is an
    ``UnsoundSecretFile`` worded by ``refusals``.

    The frame that holds the file's bytes only returns the verdict; the outer frame raises.
    """

    def judged(path: Path) -> Login:
        return assess(read_secret_file(path, "login file"))

    def read(path: Path) -> Login:
        login = judged(path)
        if login.verdict is not usable:
            raise UnsoundSecretFile(f"{shown_text(str(path))} {refusals[login.verdict]}")
        return login

    return read


read_codex_login = _login_reader(
    assess_codex_login, CodexLoginVerdict.USABLE, CODEX_LOGIN_REFUSALS
)
read_grok_login = _login_reader(assess_grok_login, GrokLoginVerdict.USABLE, GROK_LOGIN_REFUSALS)


LOGIN_READERS: Final[Mapping[Provider, Callable[[Path], object]]] = {
    Provider.ANTHROPIC: read_token_file,
    Provider.OPENAI: read_codex_login,
    Provider.XAI: read_grok_login,
}


@dataclass(frozen=True)
class LoadContext:
    """What ``load_config`` tells the models about this load; without one, every file is read."""

    read_logins: bool


def _token_file_read_by(provider: Provider) -> Callable[[object, ValidationInfo], Path]:
    """The check of one provider's token file: its absolute path, once the provider's reader
    accepts the file; a refusal is a field error.

    The login read here is dropped: a swapped file takes effect on the next run without a
    restart (decision record 0001, line 2), so only the runner's read at each run's start counts.
    A load that skips login reads (the login command's) still checks the path.
    """

    def sound(token_file: object, info: ValidationInfo) -> Path:
        path = _absolute_path_to("token file", token_file)
        shown_path(path)
        logins_skipped = isinstance(info.context, LoadContext) and not info.context.read_logins
        if not logins_skipped:
            LOGIN_READERS[provider](path)
        return path

    return sound


class SigningConfig(_Section):
    """The one key the service signs with, read once at startup (decision record 0001, A1a4)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    key: Annotated[SigningKey, BeforeValidator(_signing_key_in_file)] = Field(
        validation_alias="key_file"
    )


VERSION_PATTERN: Final = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", re.ASCII)
MALFORMED_PIN: Final = (
    "must be three dot-separated integers without leading zeros, written as a string,"
    " such as 2.1.302"
)


def _pin_in_grammar(value: object) -> str:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(MALFORMED_PIN)
    return value


PinnedVersion = Annotated[str, BeforeValidator(_pin_in_grammar)]


def _program(path: object) -> Path:
    return _absolute_path_to("program", path)


def _model_part_in_grammar(name: str) -> str:
    if MODEL_PART_PATTERN.fullmatch(name) is None:
        raise ValueError("must be a model name from a-z, 0-9, . and -, with no provider or version")
    return name


ListedModel = Annotated[str, Field(strict=True), AfterValidator(_model_part_in_grammar)]


class ProgramPin(_Section):
    """One provider's program, the exact version it must report and the models it serves.

    ``models`` lists what this installation runs on the subscription; a configured
    model of this provider that this list does not name stops startup.
    """

    binary: Annotated[Path, BeforeValidator(_program)]
    version: PinnedVersion
    models: frozenset[ListedModel]


class ClaudeProgram(ProgramPin):
    """Claude's program and the setup token its runs log in with."""

    token_file: Annotated[Path, BeforeValidator(_token_file_read_by(Provider.ANTHROPIC))]


class CodexProgram(ProgramPin):
    """Codex's program and the sign-in document its runs log in with."""

    token_file: Annotated[Path, BeforeValidator(_token_file_read_by(Provider.OPENAI))]


def _private_working_root(value: object) -> PrivateWorkingRoot:
    """Constructs the root the string names; only the closed violation reaches the message."""
    if not isinstance(value, str):
        raise _not_a_path_to("working root")
    try:
        return PrivateWorkingRoot(Path(value))
    except WorkingRootNotPrivateError as error:
        raise ValueError(f"not private: {error.violation.value}") from None


class GrokProgram(ProgramPin):
    """Grok's program and the sign-in document its runs log in with."""

    token_file: Annotated[Path, BeforeValidator(_token_file_read_by(Provider.XAI))]


ProviderProgram = ClaudeProgram | CodexProgram | GrokProgram


class CliConfig(_Section):
    """The private root every program run lives in, and each provider's pinned program."""

    working_root: Annotated[PrivateWorkingRoot, PlainValidator(_private_working_root)] = Field(
        exclude=True, repr=False
    )
    anthropic: ClaudeProgram | None = None
    openai: CodexProgram | None = None
    xai: GrokProgram | None = None

    def programs(self) -> Mapping[Provider, ProviderProgram]:
        """The pinned programs this installation sets, in ``Provider`` order."""
        tables = {provider: getattr(self, provider.value, None) for provider in Provider}
        return {provider: pin for provider, pin in tables.items() if isinstance(pin, ProgramPin)}


def _ca_bundle(path: object) -> Path:
    return _absolute_path_to("CA bundle", path)


def _completion_url(url: str) -> str:
    matched = re.fullmatch(
        r"[Hh][Tt][Tt][Pp][Ss]?://(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)(?::(\d+))?/v1/chat/completions",
        url, re.ASCII,
    )
    if matched is not None:
        host, port = matched.groups()
        if _gateway_host(host) and (port is None or 1 <= int(port) <= 65535):
            return url
    raise ValueError("must be an ASCII HTTP(S) URL ending exactly in /v1/chat/completions")


def _gateway_host(host: str) -> bool:
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return False
        return True
    return len(host) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host.removesuffix(".").split(".")
    )


def _credential_file(path: object) -> Path:
    return _absolute_path_to("API credential file", path)


class ApiConfig(_Section):
    """The inference-only gateway and the file selecting its credential for each app."""

    completion_url: Annotated[NonEmptyText, AfterValidator(_completion_url)]
    credential_file: Annotated[Path, BeforeValidator(_credential_file)]
    ca_file: Annotated[Path, BeforeValidator(_ca_bundle)] | None = None

    @model_validator(mode="after")
    def _tls_has_explicit_trust(self) -> Self:
        if self.completion_url.lower().startswith("https:") != (self.ca_file is not None):
            raise ValueError("api.ca_file: required for HTTPS and forbidden for HTTP")
        return self


class McpConfig(_Section):
    """The one CA bundle that signs the certificates of the apps' MCP origins."""

    ca_file: Annotated[Path, BeforeValidator(_ca_bundle)] | None = None


class ServerConfig(_Section):
    host: NonEmptyText = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


MAX_TOOL_TIMEOUT_MS: Final = 2_147_483_647


class RunConfig(_Section):
    """How a run's pace, length and volume are limited (decision record 0001, H6).

    Strict numbers: TOML ``true`` is not a heartbeat of 1 second and ``15.5``
    is not a heartbeat of 15. The heartbeat is announced on the wire, whose
    bound is 1..300. ``max_files_bytes`` limits all files of one run together;
    the wire's ceiling bounds it.
    """

    heartbeat_interval_s: StrictInt = Field(default=15, ge=1, le=300)
    stall_timeout_s: StrictFloat = Field(default=30.0, gt=0, allow_inf_nan=False)
    idle_timeout_s: StrictFloat = Field(default=120.0, gt=0, allow_inf_nan=False)
    max_duration_s: StrictFloat = Field(
        default=600.0, gt=0, le=MAX_TOOL_TIMEOUT_MS // 1000, allow_inf_nan=False
    )
    max_tool_calls: StrictInt = Field(default=30, ge=1)
    max_bytes: StrictInt = Field(default=4 * MAX_EVENT_BYTES, ge=1)
    max_files_bytes: StrictInt = Field(default=8 * 1024 * 1024, ge=1, le=MAX_RUN_FILES_BYTES)
    max_parallel_cli_runs: StrictInt = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _heartbeat_before_stall_before_idle_before_maximum(self) -> Self:
        if not (
            self.heartbeat_interval_s
            < self.stall_timeout_s
            <= self.idle_timeout_s
            < self.max_duration_s
        ):
            raise ValueError(
                "must satisfy heartbeat_interval_s < stall_timeout_s <= idle_timeout_s"
                " < max_duration_s (got"
                f" {self.heartbeat_interval_s}, {self.stall_timeout_s},"
                f" {self.idle_timeout_s}, {self.max_duration_s})"
            )
        return self


class DefaultsConfig(_Section):
    """What a run uses when neither its agent's nor its app's table names it."""

    model: ModelName
    cli_settings: CliSettingsConfig = CliSettingsConfig()


class AgentConfig(_Section):
    """One of an app's agents; ``runs_as_app`` lets it run on the app's workload token alone."""

    runs_as_app: StrictBool = False
    model: ModelName | None = None
    cli_settings: CliSettingsConfig = CliSettingsConfig()


class AppConfig(_Section):
    """One app: its workload client, its MCP origin and the agents the operator names.

    The app's tools and their effects arrive with each run request; ``model``, when set, is the
    model of the app's runs and is validated at startup like the default.
    """

    workload_client: NonEmptyText
    model: ModelName | None = None
    mcp_origin: McpOrigin | None = None
    agents: Mapping[AgentName, AgentConfig] = {}
    cli_settings: CliSettingsConfig = CliSettingsConfig()

    def runs_as_itself(self, agent: str) -> bool:
        agent_config = self.agents.get(agent)
        return agent_config is not None and agent_config.runs_as_app

    @property
    def has_an_agent_that_runs_as_itself(self) -> bool:
        return any(agent.runs_as_app for agent in self.agents.values())


def _named_tables(
    document: object, section: str, built_model: type[_Section]
) -> Iterator[tuple[str, object]]:
    """The named tables of a section, raw or already built; other shapes are left to the fields."""
    if not isinstance(document, Mapping) or not isinstance(document.get(section), Mapping):
        return
    for name, table in document[section].items():
        if isinstance(name, str) and isinstance(table, Mapping | built_model):
            yield name, table


def _value(table: object, key: str) -> object:
    """What pydantic will see under ``key``: ``None`` is absent, in a mapping or a built model."""
    if isinstance(table, Mapping):
        return table.get(key)
    return getattr(table, key)


RELAY_ATTACHED_PROVIDERS: Final = frozenset({Provider.ANTHROPIC})


def _refuse_an_origin_without_signing(document: object) -> None:
    if not isinstance(document, Mapping) or document.get("signing") is not None:
        return
    for app_name, app in _named_tables(document, "apps", AppConfig):
        if _value(app, "mcp_origin") is not None:
            raise ValueError(
                f"apps.{shown_text(app_name)}.mcp_origin: needs a [signing] section,"
                " because the app's runs carry a signed capability"
            )


def _declares_an_origin(app: object) -> bool:
    return _value(app, "mcp_origin") is not None


def _refuse_an_origin_without_a_ca_bundle(document: object) -> None:
    mcp = _value(document, "mcp")
    if isinstance(mcp, Mapping | McpConfig) and _value(mcp, "ca_file") is not None:
        return
    for app_name, app in _named_tables(document, "apps", AppConfig):
        if _declares_an_origin(app):
            raise ValueError(
                f"mcp.ca_file: required because apps.{shown_text(app_name)} declares an mcp_origin"
            )


RULES_ON_THE_RAW_DOCUMENT: Final = (
    _refuse_an_origin_without_signing,
    _refuse_an_origin_without_a_ca_bundle,
)


_CLI_SETTINGS_LEVEL: Final[Mapping[Provider, Callable[[CliSettingsConfig], CliSettings]]] = {
    Provider.ANTHROPIC: lambda table: table.anthropic.as_level(),
    Provider.OPENAI: lambda table: cast("CliSettings", table.openai),
    Provider.XAI: lambda table: cast("CliSettings", table.xai),
}


class ServiceConfig(_Section):
    identity: IdentityConfig
    defaults: DefaultsConfig
    server: ServerConfig = ServerConfig()
    runs: RunConfig = RunConfig()
    cli: CliConfig
    api: ApiConfig | None = None
    mcp: McpConfig | None = None
    signing: SigningConfig | None = None
    apps: dict[NonEmptyText, AppConfig]
    _admissions: dict[str, AdmissionConfig] = PrivateAttr(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _relationships_that_need_no_secret(cls, document: object) -> object:
        """Decided on the raw document, so no token or key is loaded when a refusal is raised."""
        for refuse_if_broken in RULES_ON_THE_RAW_DOCUMENT:
            refuse_if_broken(document)
        return document

    @model_validator(mode="after")
    def _every_configured_model_is_listed_by_its_cli_table(self) -> Self:
        listed = {
            f"{provider}/{model}"
            for provider, models in self.subscription_models().items()
            for model in models
        }
        for field, name in self._configured_models():
            if name not in listed:
                raise ValueError(
                    f"{field}: {shown_text(name)} is not listed in the models of any"
                    " [cli.<provider>] table"
                )
        return self

    def _configured_models(self) -> Iterator[tuple[str, str]]:
        yield "defaults.model", self.defaults.model
        for app_name, app in self.apps.items():
            app_field = f"apps.{shown_text(app_name)}"
            if app.model is not None:
                yield f"{app_field}.model", app.model
            for agent_name, agent in app.agents.items():
                if agent.model is not None:
                    yield f"{app_field}.agents.{shown_text(agent_name)}.model", agent.model

    @model_validator(mode="after")
    def _api_groups_and_the_api_section_come_together(self) -> Self:
        if self.identity.api_groups and self.api is None:
            raise ValueError("api: required because identity.api_groups is set")
        if self.api is not None:
            if not self.identity.api_groups:
                raise ValueError("identity.api_groups: required because [api] is set")
            read_api_credentials(self.api.credential_file, frozenset(self.apps))
        return self

    @model_validator(mode="after")
    def _workload_clients_are_unique(self) -> Self:
        owners: dict[str, str] = {}
        for app_name, app in self.apps.items():
            owner = owners.setdefault(app.workload_client, app_name)
            if owner != app_name:
                raise ValueError(
                    f"apps.{shown_text(app_name)}.workload_client:"
                    f" {app.workload_client!r} is already"
                    f" the workload client of app {owner!r}"
                )
        return self

    @model_validator(mode="after")
    def _origins_are_unique(self) -> Self:
        owners: dict[str, str] = {}
        for app_name, app in self.apps.items():
            if app.mcp_origin is None:
                continue
            owner = owners.setdefault(app.mcp_origin, app_name)
            if owner != app_name:
                raise ValueError(
                    f"apps.{shown_text(app_name)}.mcp_origin: {app.mcp_origin} is already"
                    f" the MCP origin of app {owner!r}"
                )
        return self

    @model_validator(mode="after")
    def _app_names_are_group_segments(self) -> Self:
        for app_name in self.apps:
            try:
                self._admissions[app_name] = self._admission_of(app_name)
            except ValueError as error:
                raise ValueError(
                    f"apps.{shown_text(app_name)}: not one group path segment: "
                    f"{shown_text(str(error))}"
                ) from error
        return self

    def model_for(self, app_name: str, agent: str) -> ModelName:
        """The model of the app's run of ``agent``: the agent's, else the app's, else defaults."""
        app = self.apps[app_name]
        agent_config = app.agents.get(agent)
        if agent_config is not None and agent_config.model is not None:
            return agent_config.model
        return app.model if app.model is not None else self.defaults.model

    def cli_settings_levels(
        self, app_name: str, agent: str, provider: Provider
    ) -> tuple[CliSettings, ...]:
        """The settings of the app's run of ``agent`` on ``provider``, lowest level first.

        The levels stay unmerged: the adapter merges them once over its own defaults.
        """
        app = self.apps[app_name]
        agent_config = app.agents.get(agent)
        level_of = _CLI_SETTINGS_LEVEL[provider]
        return (
            level_of(self.defaults.cli_settings),
            level_of(app.cli_settings),
            {} if agent_config is None else level_of(agent_config.cli_settings),
        )

    def capability_scope(self, app_name: str, tools: Iterable[str]) -> CapabilityScope | None:
        """What the app's capability is bound to, granting exactly ``tools`` sorted."""
        app = self.apps[app_name]
        if app.mcp_origin is None or self.signing is None:
            return None
        return CapabilityScope(app.mcp_origin, tuple(sorted(tools)), self.signing.key)

    def subscription_models(self) -> Mapping[Provider, frozenset[str]]:
        """The model names each provider's subscription program serves."""
        return {provider: pin.models for provider, pin in self.cli.programs().items()}

    def mcp_trust_bundle(self) -> Path | None:
        """The CA bundle for the apps' MCP origins; ``None`` while no app declares an origin."""
        if self.mcp is None or not any(map(_declares_an_origin, self.apps.values())):
            return None
        return self.mcp.ca_file

    def app_admission(self, app_name: str) -> AdmissionConfig:
        """Who may use the app: its group ``/apps/<app>``, segment-aware, and its admins below."""
        return self._admissions[app_name]

    def _admission_of(self, app_name: str) -> AdmissionConfig:
        if "/" in app_name:
            raise ValueError("an app name is exactly one path segment")
        group = f"{APP_GROUP_PREFIX}{app_name}"
        return AdmissionConfig(
            bound_issuer=self.identity.issuer,
            login_group=group,
            admin_group=f"{group}/admin",
        )

    def app_of_workload_client(self, client_id: str) -> str | None:
        return self._apps_by_workload_client.get(client_id)

    @cached_property
    def _apps_by_workload_client(self) -> dict[str, str]:
        return {app.workload_client: app_name for app_name, app in self.apps.items()}


def load_config(path: Path, *, read_logins: bool = True) -> ServiceConfig:
    """The configuration the file holds; only the login command loads without reading logins."""
    try:
        text = path.read_text(encoding="utf-8")  # NOSONAR(S8707) the operator names this file
    except OSError as error:
        raise ConfigError(f"{shown_text(str(path))}: cannot be read: {error.strerror}") from error
    except UnicodeDecodeError as error:
        raise ConfigError(f"{shown_text(str(path))}: not UTF-8 TOML") from error
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{shown_text(str(path))}: not TOML: {shown_text(str(error))}") from error
    try:
        return ServiceConfig.model_validate(document, context=LoadContext(read_logins))
    except ValidationError as error:
        refusal = f"{shown_text(str(path))}: {_named_fields(error)}"
    raise ConfigError(refusal)


def _named_fields(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(shown_text(str(part)) for part in problem['loc'])}: {_message(problem['msg'])}"
        if problem["loc"]
        else _message(problem["msg"])
        for problem in error.errors(include_input=False, include_url=False)
    )


def _message(pydantic_message: str) -> str:
    return pydantic_message.removeprefix("Value error, ")
