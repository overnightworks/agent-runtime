"""``agent-runtime-service-login --config PATH --provider NAME [--check]``: install a login.

Reads a provider's new login document on standard input, checks it with the service's reader for
that provider and swaps it in over the ``token_file`` of its ``[cli.<provider>]`` table through
the one login writer.
``--check`` runs that reader on the file as it stands and reads no input; it does not prove the
provider still accepts the login.

Exit 0: written (or accepted). 1: not written, the old file stays (or the file is refused).
2: refused before standard input was read. 3: not written, and the command's own sibling file is
not confirmed gone.

SIGINT, SIGTERM and SIGHUP are disregarded from hardening on until the process exits, except
while standard input is read, so no interrupt can reach a frame that holds the document or change
the status after the commit. Every line goes to descriptor 2 by ``os.write``: a failed write to a
closed stderr never changes the status.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from types import FrameType
from typing import BinaryIO, Final

from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES, UnsoundSecretFile, shown_text
from agent_runtime_service.cli import EXIT_BAD_CONFIG
from agent_runtime_service.login_file import (
    SwapRefused,
    swap_in_login_file,
    swap_sibling_or_refusal,
)
from agent_runtime_service.process_hardening import StartupHardeningRefused, harden_process

EXIT_NOT_WRITTEN: Final = 1
EXIT_REMAINS: Final = 3
PROGRAM_NAME: Final = "agent-runtime-service-login"
INTERRUPT_SIGNAL_NAMES: Final = ("SIGINT", "SIGTERM", "SIGHUP")


def _disregard(signal_number: int, frame: FrameType | None) -> None:
    return None


def interrupt_signals() -> tuple[int, ...]:
    """Resolved on use: ``SIGHUP`` does not exist everywhere, and the platform gate runs first."""
    return tuple(getattr(signal, name) for name in INTERRUPT_SIGNAL_NAMES)


def disregard_interrupts() -> None:
    """Catch the three signals with a handler that returns.

    A caught signal, unlike an ignored one, is reset to its default by ``exec``, so a child
    started inside the window stays interruptible.
    """
    for signal_number in interrupt_signals():
        signal.signal(signal_number, _disregard)


def _interrupt_once(signal_number: int, frame: FrameType | None) -> None:
    disregard_interrupts()
    raise KeyboardInterrupt


def _say(line: str) -> None:
    """One line on descriptor 2; a write that fails drops the line and never the status."""
    remaining = f"{PROGRAM_NAME}: {line}\n".encode(errors="replace")
    try:
        while remaining:
            remaining = remaining[os.write(2, remaining) :]
    except OSError:
        return


def _document_read(standard_input: BinaryIO | None) -> bytes | SwapRefused:
    """The only window where an interrupt acts; the document or a closed reason."""
    if standard_input is None:
        return SwapRefused("standard input cannot be read", False)
    try:
        for signal_number in interrupt_signals():
            signal.signal(signal_number, _interrupt_once)
        try:
            return standard_input.read(MAX_SECRET_FILE_BYTES + 1)
        finally:
            disregard_interrupts()
    except KeyboardInterrupt:
        return SwapRefused("interrupted while reading standard input", False)
    except OSError as error:
        return SwapRefused(f"standard input cannot be read: {error.strerror}", False)
    except Exception as error:
        return SwapRefused(f"failed: {type(error).__name__}", False)


def _read_and_swapped(
    standard_input: BinaryIO | None, token_file: Path, read_login: Callable[[Path], object]
) -> SwapRefused | None:
    """Reads the document and swaps it in; returns closed values only."""
    document = _document_read(standard_input)
    if isinstance(document, SwapRefused):
        return document
    return swap_in_login_file(token_file, document, read_login)


def _checked(subject: str, token_file: Path, read_login: Callable[[Path], object]) -> int:
    try:
        read_login(token_file)
    except UnsoundSecretFile as refusal:
        _say(f"{subject}: {refusal}")
        return EXIT_NOT_WRITTEN
    _say(
        f"{subject}: accepted by the service's reader;"
        " whether the provider still accepts it shows only in a run"
    )
    return 0


def _sibling_left_over(sibling: Path) -> bool:
    """Whether a sibling may exist; only ``FileNotFoundError`` establishes that none does."""
    try:
        os.lstat(sibling)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    return True


def _installed(
    subject: str,
    token_file: Path,
    read_login: Callable[[Path], object],
    standard_input: BinaryIO | None,
) -> int:
    sibling = swap_sibling_or_refusal(token_file)
    if isinstance(sibling, SwapRefused):
        _say(f"{subject}: not written: {sibling.reason}")
        return EXIT_BAD_CONFIG
    if _sibling_left_over(sibling):
        _say(
            f"{shown_text(str(sibling))} exists from an earlier install and may"
            " hold a login; remove it, then run this again"
        )
        return EXIT_BAD_CONFIG
    refused = _read_and_swapped(standard_input, token_file, read_login)
    if refused is None:
        _say(f"{subject}: written")
        return 0
    _say(f"{subject}: not written: {refused.reason}")
    return EXIT_REMAINS if refused.sibling_remains else EXIT_NOT_WRITTEN


def _arguments(argv: Sequence[str] | None, providers: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=PROGRAM_NAME, description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="the service's TOML file")
    parser.add_argument(
        "--provider",
        required=True,
        choices=sorted(providers),
        help="the provider whose login this is",
    )
    parser.add_argument("--check", action="store_true", help="check the file; read no input")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, standard_input: BinaryIO | None = None) -> int:
    try:
        harden_process()
    except StartupHardeningRefused as error:
        _say(str(error))
        return EXIT_BAD_CONFIG
    disregard_interrupts()
    # Imported only now, as the service's start does: its modules need Unix-only constants.
    from agent_runtime_service.config import LOGIN_READERS, ConfigError, Provider, load_config

    arguments = _arguments(argv, Provider)
    try:
        config = load_config(arguments.config, read_logins=False)
    except ConfigError as error:
        _say(str(error))
        return EXIT_BAD_CONFIG
    provider = Provider(arguments.provider)
    table = config.cli.programs().get(provider) if config.cli is not None else None
    if table is None:
        _say(f"cli.{arguments.provider}: no such table")
        return EXIT_BAD_CONFIG
    subject = f"cli.{arguments.provider}.token_file"
    read_login = LOGIN_READERS[provider]
    if arguments.check:
        return _checked(subject, table.token_file, read_login)
    return _installed(
        subject,
        table.token_file,
        read_login,
        sys.stdin.buffer if standard_input is None and sys.stdin is not None else standard_input,
    )
