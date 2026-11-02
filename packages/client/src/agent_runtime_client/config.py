"""The ``agent`` section every app embeds in its own configuration."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, model_validator

from agent_runtime_client.turn import NonEmptyUnicodeText


class BackendKind(StrEnum):
    """Which backend an app runs its agent turns through."""

    SERVICE = "service"
    LITELLM = "litellm"


class AgentConfig(BaseModel):
    """What an app tells the connector: its backend, the service's URL and its workload scope.

    Every field is a host fact, so none has a default and a malformed value fails loud.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: BackendKind
    service_url: AnyHttpUrl
    workload_scope: NonEmptyUnicodeText

    @model_validator(mode="after")
    def _refuse_backend_this_release_lacks(self) -> Self:
        if self.backend is BackendKind.LITELLM:
            raise ValueError(
                "`litellm` needs agent-runtime's chat-completions backend, "
                "which this release does not include"
            )
        return self
