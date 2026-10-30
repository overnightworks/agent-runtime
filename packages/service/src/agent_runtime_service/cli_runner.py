"""The CLI route's runner: the provider a route names, run through the core with its tools.

``CliRunner.run`` builds the provider run of the route's provider, Claude's, Codex's or Grok's.
It first reads the provider table's login file with that provider's reader, so a swapped file takes
effect on the next run without a restart, and a file gone bad fails that run as an
``UnsoundSecretFile`` before any tool connection or child (decision record 0001, lines 2 and
14); the secret file's size limit bounds that read, so it stays on the event loop. The
provider's factory takes the launch and reads the login; the run it returns takes only the turn,
the run's model and the relay's access, so no factory sees the admitted run or its capability.
When the run's capability grants tools it then opens the run's tool set (``RunTools``), hands
the adapter only the relay's address and secret, and merges the relay's tool events with the
core's text and ending (``RelayedEvents``); the CLI's own tool lines are dropped. Its one exit
path closes the tool set once, with the core's reap, so a login refusal, a failed exit, a
barrier refusal or a lost containment reaches the service's run supervision as it was raised. It
reads no installed core configuration: each fact arrives as an argument.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from agent_runtime.claude.run import ClaudeRun
from agent_runtime.cli_run import CliRunAdapter, RelayAccess, astream_cli_run
from agent_runtime.cli_settings import CliSettings
from agent_runtime.codex.run import CodexRun
from agent_runtime.codex_login import CodexLogin
from agent_runtime.config import PrivateWorkingRoot
from agent_runtime.events import StreamEvent
from agent_runtime.grok.run import GrokRun
from agent_runtime.grok_login import GrokLogin
from agent_runtime.secret_file import read_token_file
from agent_runtime_client.wire import Turn
from agent_runtime_service.config import Provider, ServiceConfig, read_codex_login, read_grok_login
from agent_runtime_service.relayed_events import RelayedEvents
from agent_runtime_service.routing import Cli, split_model
from agent_runtime_service.run_liveness import RunClock
from agent_runtime_service.run_tools import RunTools
from agent_runtime_service.runners import AdmittedRun


@dataclass(frozen=True)
class ProviderLaunch:
    provider: Provider
    binary: Path
    token_file: Path
    max_tool_calls: int
    tool_timeout_seconds: float


@dataclass(frozen=True)
class RunInputs:
    """All of a run an adapter may see: its turn, model, relay access and settings levels."""

    turn: Turn
    model: str
    relay: RelayAccess | None
    settings: tuple[CliSettings, ...]


LoggedInRun = Callable[[RunInputs], CliRunAdapter]
AdapterFactory = Callable[[ProviderLaunch], LoggedInRun]


def _claude_run(launch: ProviderLaunch, token: SecretStr, inputs: RunInputs) -> CliRunAdapter:
    return ClaudeRun(
        binary=launch.binary,
        token=token,
        system=inputs.turn.system,
        max_tool_calls=launch.max_tool_calls,
        history=tuple(inputs.turn.messages),
        relay=inputs.relay,
        tool_timeout_seconds=launch.tool_timeout_seconds,
        model=inputs.model,
        settings=inputs.settings,
    )


def claude_run(launch: ProviderLaunch) -> LoggedInRun:
    return partial(_claude_run, launch, SecretStr(read_token_file(launch.token_file)))


def _refused_relay(provider: str, inputs: RunInputs) -> None:
    if inputs.relay is not None:
        raise ValueError(
            f"a {provider} run has no tool relay; admission refuses tools before this point"
        )


def _codex_run(launch: ProviderLaunch, login: CodexLogin, inputs: RunInputs) -> CliRunAdapter:
    _refused_relay("Codex", inputs)
    return CodexRun(
        launch.binary,
        login,
        inputs.turn.system,
        tuple(inputs.turn.messages),
        model=inputs.model,
        settings=inputs.settings,
    )


def codex_run(launch: ProviderLaunch) -> LoggedInRun:
    return partial(_codex_run, launch, read_codex_login(launch.token_file))


def _grok_run(launch: ProviderLaunch, login: GrokLogin, inputs: RunInputs) -> CliRunAdapter:
    _refused_relay("Grok", inputs)
    return GrokRun(
        launch.binary,
        login,
        inputs.turn.system,
        tuple(inputs.turn.messages),
        model=inputs.model,
    )


def grok_run(launch: ProviderLaunch) -> LoggedInRun:
    return partial(_grok_run, launch, read_grok_login(launch.token_file))


RUN_ADAPTERS: Final[Mapping[Provider, AdapterFactory]] = {
    Provider.ANTHROPIC: claude_run,
    Provider.OPENAI: codex_run,
    Provider.XAI: grok_run,
}


def _launches_of(config: ServiceConfig) -> Mapping[Provider, ProviderLaunch]:
    return {
        provider: ProviderLaunch(
            provider=provider,
            binary=program.binary,
            token_file=program.token_file,
            max_tool_calls=config.runs.max_tool_calls,
            tool_timeout_seconds=config.runs.max_duration_s,
        )
        for provider, program in config.cli.programs().items()
    }


def mcp_tls_context(ca_file: Path) -> ssl.SSLContext:
    """The context that trusts exactly the bundle ``ca_file`` names.

    Not ``ssl.create_default_context()``: that one honours ``SSL_CERT_FILE`` and
    ``SSLKEYLOGFILE``, which would let the environment widen the trust or log the app's keys.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(ca_file))
    return context


class CliRunner:
    def __init__(
        self, config: ServiceConfig, *, tls_context: ssl.SSLContext | None, clock: RunClock
    ) -> None:
        self._working_root: PrivateWorkingRoot = config.cli.working_root
        self._launches = _launches_of(config)
        self._tls_context = tls_context
        self._clock = clock
        self._max_files_bytes = config.runs.max_files_bytes
        self._settings_levels = config.cli_settings_levels

    async def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]:
        assert isinstance(admitted.route, Cli)  # noqa: S101 - Runners.for_route sends only Cli routes here
        tools: RunTools | None = None
        events: RelayedEvents | None = None
        try:
            launch = self._launches[admitted.route.provider]
            logged_in = RUN_ADAPTERS[launch.provider](launch)
            tools = await self._opened_tools(admitted)
            adapter = logged_in(
                RunInputs(
                    admitted.request.turn,
                    split_model(admitted.model)[1],
                    None if tools is None else tools.access,
                    self._settings_levels(
                        admitted.app, admitted.request.agent, admitted.route.provider
                    ),
                )
            )
            events = RelayedEvents(
                astream_cli_run(
                    self._working_root, adapter, max_files_bytes=self._max_files_bytes
                ),
                None if tools is None else tools.calls,
            )
            async for event in events:
                yield event
        finally:
            await self._finish(tools, events)

    async def _opened_tools(self, admitted: AdmittedRun) -> RunTools | None:
        capability = admitted.capability
        if capability is None or not capability.claims.tools:
            return None
        assert self._tls_context is not None  # noqa: S101 - a capability that claims tools exists only with a TLS context
        return await RunTools.open(
            str(admitted.run_id), capability.claims.aud, capability, self._tls_context, self._clock
        )

    @staticmethod
    async def _finish(tools: RunTools | None, events: RelayedEvents | None) -> None:
        reap = events.close if events is not None else partial(asyncio.sleep, 0)
        if tools is None:
            await reap()
        else:
            await tools.close(reap)
