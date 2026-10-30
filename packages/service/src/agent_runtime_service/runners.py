"""The port through which an admitted run reaches a provider.

A runner turns one admitted run into the provider core's in-process turn
events (``agent_runtime.events``); the service maps those onto the wire.
Each route has at most one runner. Production always installs the CLI runner and installs
the API runner when ``[api]`` is set; an admitted run always has its route's runner.

An admitted run of an app with an MCP origin carries its capability
(``agent_runtime_service.capability``), minted at admission; a runner that sends it
keeps it out of everything it yields and logs. ``run`` must not raise before it
returns its generator; an async generator function satisfies this.

A runner must end when its step is cancelled or its generator is closed, and
its close must finish in bounded time: the service ends a silent or overlong
run by cancelling the step and waiting for the close (``run_liveness``), and
never leaves a runner behind.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from webauth.service_callers import Principal

from agent_runtime.events import StreamEvent
from agent_runtime_client.wire import ModelName, RunRequest
from agent_runtime_service.capability import Capability
from agent_runtime_service.routing import Api, Cli, ServedRoute


@dataclass(frozen=True)
class AdmittedRun:
    run_id: UUID
    app: str
    principal: Principal
    request: RunRequest
    model: ModelName
    route: ServedRoute
    capability: Capability | None


class ApiUnavailable(Exception):
    """The API gateway or its credential is unavailable; contains no upstream exception."""

    def __init__(self) -> None:
        super().__init__("The API provider is unavailable.")


class Runner(Protocol):
    def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]: ...


@dataclass(frozen=True)
class Runners:
    cli: Runner
    api: Runner | None = None

    def for_route(self, served: ServedRoute) -> Runner:
        match served:
            case Cli():
                return self.cli
            case Api():
                assert self.api is not None  # noqa: S101 - the host routes to Api only when it installed an api runner
                return self.api
