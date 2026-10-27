"""Shared arrangement of the ``agent-runtime-service-login`` tests: a provider table logging in from
``<tmp>/login/<file>``, the stream standing for standard input, and the signal helpers.

The canaries stand for secrets that no line or held exception may carry.
"""

from __future__ import annotations

import errno
import io
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from provider_test_support import a_codex_login, write_claude_token_file
from service_support import TEST_ISSUER, _service_config_document, reaches_text
from test_service_codex_profile import (
    a_service_fake,
    codex_document,
    write_login,
    written,
)

import agent_runtime_service.config as service_config
import agent_runtime_service.login as login
from agent_runtime_service.config import Provider
from agent_runtime_service.login import main
from agent_runtime_service.login_file import SwapRefused, swap_in_login_file, swap_sibling

CANARY_REFRESH = "canary-refresh-token"
CANARY_FOREIGN = "canary-foreign-text"
OLD_CLAUDE_TOKEN = "old-token\n"
NEW_CLAUDE_TOKEN = b"new-token\n"
LINE_PREFIX = "agent-runtime-service-login: "
LOGIN_NAME = "auth.json"
CODEX_FILE_REFUSAL = "is not a complete Codex sign-in"
RUN_THE_COMMAND = "import sys; from agent_runtime_service.login import main; sys.exit(main())"


@dataclass(frozen=True)
class Installation:
    """A configuration whose provider table logs in from ``login`` in a directory of its own."""

    config: Path
    provider: Provider
    login: Path
    old_bytes: bytes
    old_inode: int

    @property
    def sibling(self) -> Path:
        return swap_sibling(self.login)

    def command(self, *extra: str) -> list[str]:
        return ["--config", str(self.config), "--provider", self.provider.value, *extra]

    def unchanged(self) -> bool:
        status = self.login.stat()
        return (self.login.read_bytes(), status.st_ino) == (self.old_bytes, self.old_inode)

    def directory_holds(self) -> list[str]:
        return sorted(entry.name for entry in self.login.parent.iterdir())

    def snapshot(self) -> tuple[bytes | None, int | None, list[str]]:
        """The login's bytes and inode (``None`` when absent) and the directory listing."""
        if not self.login.exists():
            return None, None, self.directory_holds()
        return self.login.read_bytes(), self.login.stat().st_ino, self.directory_holds()


def an_installation(tmp_path: Path, provider: Provider, *, old: bool = True) -> Installation:
    directory = tmp_path / "login"
    directory.mkdir()
    path = directory / LOGIN_NAME
    if provider is Provider.CODEX:
        if old:
            write_login(path, a_codex_login(tokens={"access_token": "old-access-token"}))
        document = codex_document(tmp_path, a_service_fake(tmp_path), path)
    else:
        if old:
            write_claude_token_file(path, OLD_CLAUDE_TOKEN.strip())
        document = _service_config_document(TEST_ISSUER, None, None, token_file=path)
    config = written(tmp_path, document)
    status = path.stat() if old else None
    return Installation(
        config,
        provider,
        path,
        path.read_bytes() if old else b"",
        status.st_ino if status else 0,
    )


@pytest.fixture(
    name="installation", params=[Provider.CODEX, Provider.CLAUDE], ids=["codex", "claude"]
)
def a_login_installation(
    request: pytest.FixtureRequest, tmp_path: Path, restored_signal_handlers: None
) -> Installation:
    return an_installation(tmp_path, request.param)


GOOD_DOCUMENTS = {
    Provider.CODEX: a_codex_login(tokens={"refresh_token": CANARY_REFRESH}),
    Provider.CLAUDE: NEW_CLAUDE_TOKEN,
}


class Said:
    """What the command wrote to descriptor 2 and to ``sys.stderr``."""

    def __init__(self, capfd: pytest.CaptureFixture[str]) -> None:
        self._capfd = capfd
        self.stream = io.StringIO()

    @property
    def descriptor(self) -> str:
        return self._capfd.readouterr().err

    @property
    def stderr(self) -> str:
        return self.stream.getvalue()

    @contextmanager
    def watching(self) -> Iterator[None]:
        """``sys.stderr`` is the recording stream inside; pytest resets it before each call."""
        original, sys.stderr = sys.stderr, self.stream
        try:
            yield
        finally:
            sys.stderr = original


@pytest.fixture(name="said")
def what_was_said(capfd: pytest.CaptureFixture[str], restored_signal_handlers: None) -> Said:
    return Said(capfd)


class Stream(io.BytesIO):
    """Standard input that runs a hook at each read and remembers that it was read."""

    def __init__(self, document: bytes, before_read: Callable[[], None] | None = None) -> None:
        super().__init__(document)
        self.before_read = before_read
        self.was_read = False

    def read(self, size: int | None = -1) -> bytes:
        self.was_read = True
        if self.before_read is not None:
            self.before_read()
        return super().read(size)


@pytest.fixture(name="refusals")
def what_the_writer_returned(monkeypatch: pytest.MonkeyPatch) -> list[SwapRefused | None]:
    """What the writer returned at each call, for the check that none holds a canary."""
    returned: list[SwapRefused | None] = []

    def recording(*arguments: Any) -> SwapRefused | None:
        returned.append(swap_in_login_file(*arguments))
        return returned[-1]

    monkeypatch.setattr(login, "swap_in_login_file", recording)
    return returned


@pytest.fixture(name="restored_signal_handlers")
def the_signal_handlers_restored() -> Iterator[None]:
    """The command keeps the three signals disregarded until its process exits; a test does not."""
    previous = {number: signal.getsignal(number) for number in login.interrupt_signals()}
    yield
    for number, handler in previous.items():
        signal.signal(number, handler)


@pytest.fixture(name="recorded_signals")
def the_signals_received(restored_signal_handlers: None) -> Callable[[], list[int]]:
    """Recording handlers; the command must replace them before any signal arrives."""
    received: list[int] = []

    def record(number: int, frame: object) -> None:
        received.append(number)

    for number in login.interrupt_signals():
        signal.signal(number, record)
    return lambda: received


def send(*numbers: int) -> None:
    for number in numbers:
        signal.pthread_kill(threading.get_ident(), number)


def send_while_blocked(*numbers: int) -> None:
    """Deliver the signals together, as two arriving at once."""
    signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    send(*numbers)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, numbers)


def reader_hook(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, hook: Callable[[], None]
) -> None:
    """The provider's reader, run on the sibling, calls the hook first."""
    real = service_config.LOGIN_READERS[provider]

    def reading(path: Path) -> object:
        hook()
        return real(path)

    monkeypatch.setitem(service_config.LOGIN_READERS, provider, reading)


def an_old_file_fails(monkeypatch: pytest.MonkeyPatch, name: str, sibling: Path) -> None:
    """``os.<name>`` raises ``PermissionError`` for the sibling and works for every other path."""
    real = getattr(os, name)

    def refusing(path: Any, *rest: Any) -> Any:
        if Path(path) == sibling:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real(path, *rest)

    monkeypatch.setattr(os, name, refusing)


def run(
    installation: Installation, stream: io.BytesIO, said: Said, *extra: str
) -> tuple[int, str]:
    with said.watching():
        status = main(installation.command(*extra), stream)
    return status, said.descriptor


def line_of(installation: Installation, text: str) -> str:
    return f"{LINE_PREFIX}cli.{installation.provider}.token_file: {text}"


def assert_not_written(
    installation: Installation,
    status: int,
    heard: str,
    refusals: list[SwapRefused | None],
    canaries: tuple[str, ...],
    reason: str,
) -> None:
    assert status == 1
    assert installation.unchanged()
    assert installation.directory_holds() == [LOGIN_NAME]
    assert line_of(installation, "not written: ") in heard
    assert reason in heard
    assert not any(canary in heard for canary in canaries)
    assert not reaches_text(refusals, canaries)
