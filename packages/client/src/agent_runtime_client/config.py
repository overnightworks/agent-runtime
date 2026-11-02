"""The ``agent`` section every app embeds in its own configuration."""

from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from agent_runtime_client.turn import NonEmptyUnicodeText

_ORIGIN: Final = re.compile(
    r"(https?)://(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)(?::(\d+))?", re.ASCII | re.IGNORECASE
)
_HOST_LABEL: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


class BackendKind(StrEnum):
    """Which backend an app runs its agent turns through."""

    LITELLM = "litellm"


def _is_host(host: str) -> bool:
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return False
        return True
    return len(host) <= 253 and all(
        _HOST_LABEL.fullmatch(label) for label in host.removesuffix(".").split(".")
    )


def _origin(origin: str) -> str:
    matched = _ORIGIN.fullmatch(origin)
    if matched is not None:
        _, host, port = matched.groups()
        if _is_host(host) and (port is None or 1 <= int(port) <= 65535):
            return origin
    raise ValueError(
        "must be an ASCII http(s) origin: scheme, host and optional port, with no path, "
        "query, fragment or userinfo"
    )


def _absolute_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("must be an absolute path; ~ is not expanded")
    return path


AbsolutePath = Annotated[Path, AfterValidator(_absolute_path)]


class ChatGateway(BaseModel):
    """The inference gateway: its origin, the file holding the app's key and the CA for HTTPS."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: Annotated[str, AfterValidator(_origin)]
    key_file: AbsolutePath
    ca_file: AbsolutePath | None = None

    @model_validator(mode="after")
    def _tls_has_explicit_trust(self) -> Self:
        if self.origin.lower().startswith("https:") != (self.ca_file is not None):
            raise ValueError("ca_file: required for HTTPS and forbidden for HTTP")
        return self


class AgentConfig(BaseModel):
    """What an app tells its backend: which backend, its gateway and its API group.

    Every field is a host fact, so none has a default and a malformed value fails loud.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: BackendKind
    gateway: ChatGateway
    api_group: NonEmptyUnicodeText
