"""``agent-runtime-service --config PATH``: harden the process, load the configuration, then serve.

A platform other than Linux, a root process, a process that cannot be made non-dumpable (so the
Claude program and anything it starts could read the service's memory), a configuration the
service refuses, or a pinned program that does not report its version ends the process
with exit status 2 and one line naming the reason; nothing is served. Otherwise it serves the
app with CLI and API runners installed independently from their sections. The CLI trusts the
CA bundle ``[mcp] ca_file`` names for the apps' MCP origins, loaded only when an app declares
one; a bundle that cannot be loaded ends the process the same way.

``--check`` takes the same path as a start and returns 0 just before it would serve, so the
check cannot drift from the start: it runs the same steps, in the same order, with the same
refusals.
"""

from __future__ import annotations

import argparse
import logging
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from agent_runtime_service.process_hardening import StartupHardeningRefused, harden_process

if TYPE_CHECKING:
    from agent_runtime_service.config import ServiceConfig
    from agent_runtime_service.run_liveness import RunClock
    from agent_runtime_service.runners import Runners


def installed_runners(
    config: ServiceConfig, tls_context: ssl.SSLContext | None, clock: RunClock
) -> Runners:
    """Install each configured route independently after process hardening."""
    from agent_runtime_service.api_runner import ApiRunner
    from agent_runtime_service.cli_runner import CliRunner
    from agent_runtime_service.runners import Runners

    return Runners(
        cli=CliRunner(config, tls_context=tls_context, clock=clock) if config.cli else None,
        api=ApiRunner(config, tls_context=tls_context, clock=clock) if config.api else None,
    )


EXIT_BAD_CONFIG: Final = 2
PROGRAM_NAME: Final = "agent-runtime-service"
REFUSAL_PREFIX: Final = f"{PROGRAM_NAME}: refusing to start: "
TRANSPORT_LOGGER_ROOTS: Final = ("httpx", "httpcore")
CLOSED_LOGGER_LEVEL: Final = logging.CRITICAL + 1


def close_transport_loggers() -> None:
    """Set the transport loggers above every call the dependencies make.

    A level fails ``isEnabledFor`` before any record exists, and a logger made later inherits
    it; a filter would act only after the record was built, and never on a child's.
    """
    children = [name for name in logging.Logger.manager.loggerDict if name.startswith("httpcore.")]
    for name in [*TRANSPORT_LOGGER_ROOTS, *children]:
        logging.getLogger(name).setLevel(CLOSED_LOGGER_LEVEL)


@dataclass(frozen=True)
class PreparedStart:
    """What a start has built once every refusal has been passed, just before serving."""

    config: ServiceConfig
    runners: Runners


def refuse(reason: str) -> int:
    print(f"{REFUSAL_PREFIX}{reason}", file=sys.stderr)
    return EXIT_BAD_CONFIG


def prepared_start(config_path: Path) -> PreparedStart | int:
    """Run every step of a start except serving; return the exit status 2 on the first refusal."""
    try:
        harden_process()
    except StartupHardeningRefused as error:
        return refuse(str(error))
    # Imported only now: the application modules need Unix-only os constants at import, so
    # importing them first would crash a non-Linux start before the refusal line.
    from agent_runtime.secret_file import shown_text
    from agent_runtime_service.cli_runner import mcp_tls_context
    from agent_runtime_service.cli_version import ProgramVersionRefused, require_pinned_versions
    from agent_runtime_service.config import ConfigError, load_config
    from agent_runtime_service.run_liveness import LoopClock

    try:
        config = load_config(config_path)
    except ConfigError as error:
        return refuse(str(error))
    try:
        if config.cli is not None:
            require_pinned_versions(config.cli)
    except ProgramVersionRefused as error:
        return refuse(f"{shown_text(str(config_path))}: {error}")
    try:
        trust_bundle = config.mcp_trust_bundle()
        tls_context = None if trust_bundle is None else mcp_tls_context(trust_bundle)
    except OSError as error:
        return refuse(f"mcp.ca_file: cannot be loaded: {shown_text(str(error))}")
    try:
        runners = installed_runners(config, tls_context, LoopClock())
    except OSError as error:
        return refuse(f"api.ca_file: cannot be loaded: {shown_text(str(error))}")
    return PreparedStart(config=config, runners=runners)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROGRAM_NAME, description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="the service's TOML file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="run every step of a start except serving, then exit 0",
    )
    arguments = parser.parse_args(argv)
    start = prepared_start(arguments.config)
    if isinstance(start, int):
        return start
    if arguments.check:
        return 0
    import uvicorn

    from agent_runtime_service.app import create_app

    close_transport_loggers()
    uvicorn.run(
        create_app(start.config, runners=start.runners),
        host=start.config.server.host,
        port=start.config.server.port,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
