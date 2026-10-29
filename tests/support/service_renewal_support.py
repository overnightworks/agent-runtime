"""Shared arrangement of the renewal attempt's tests: logins with real timing, a renewer under a
temporary directory with hand-moved clocks, and a stand-in for the bounded runner that runs a hook
on the renewal home, returns a chosen outcome and fires the end confirmation now, later or never.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from tests.support.bounds import TEST_WAIT_SECONDS
from tests.support.codex_support import a_codex_access_token, a_renewing_fake_codex
from tests.support.provider_test_support import a_codex_login, a_private_root
from tests.support.service_support import _toml_lines
from tests.support.service_table_support import CODEX_TABLE, write_login

from agent_runtime.codex.run import AUTH_FILE_NAME, CODEX_CLI_PROCESS_POOL
from agent_runtime.config import PrivateWorkingRoot
from agent_runtime.process import RUN_HOME_DIRECTORY_NAME, CliRunOutcome, CliRunReason
from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_service import renewal
from agent_runtime_service.config import Provider
from agent_runtime_service.login_file import swap_sibling
from agent_runtime_service.renewal import (
    CODEX_RENEWAL,
    ProviderRenewer,
    RenewalEntry,
    RenewalReport,
)

NOW = 1_800_000_000
LIFETIME = 864_000
HALF_LIFE = LIFETIME // 2
ORIGINAL_REFRESH = "original-refresh-token"
ROTATED_REFRESH = "rotated-refresh-token"
LOGIN_NAME = "codex-login.json"
UNUSED_BINARY = Path("/opt/codex/bin/codex")


def a_login_issued_at(
    issued_at: int,
    *,
    lifetime: int = LIFETIME,
    refresh_token: str = ORIGINAL_REFRESH,
    subject: str = "original",
) -> bytes:
    claims = {"iat": issued_at, "exp": issued_at + lifetime, "sub": subject}
    return a_codex_login(
        tokens={"access_token": a_codex_access_token(claims), "refresh_token": refresh_token}
    )


DUE_ORIGINAL = a_login_issued_at(NOW - HALF_LIFE)
RENEWED_LOGIN = a_login_issued_at(NOW, refresh_token=ROTATED_REFRESH, subject="renewed")


def a_due_login_now() -> bytes:
    """A login that is due for the real clock, as the service's own clock reads it."""
    return a_login_issued_at(int(time.time()) - HALF_LIFE - 60)


def a_renewed_login_now() -> bytes:
    return a_login_issued_at(int(time.time()), refresh_token=ROTATED_REFRESH, subject="renewed")


def a_codex_entry(deadline_seconds: float) -> RenewalEntry:
    return RenewalEntry(
        deadline_seconds, CODEX_RENEWAL.renewable, CODEX_RENEWAL.timing, CODEX_RENEWAL.run
    )


class Clocks:
    """The renewer's two clocks, moved by hand."""

    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic = 0.0


@dataclass(frozen=True)
class Arrangement:
    """A renewer for the login at ``token_file``, with the source as it was when it was built."""

    renewer: ProviderRenewer
    token_file: Path
    root: Path
    clocks: Clocks
    source_bytes: bytes
    source_inode: int

    @property
    def sibling(self) -> Path:
        return swap_sibling(self.token_file)

    def source_is_intact(self) -> bool:
        """The same bytes, the same inode, and no sibling left beside the login."""
        status = self.token_file.stat()
        return (
            self.token_file.read_bytes() == self.source_bytes
            and status.st_ino == self.source_inode
            and self.directory_holds() == [LOGIN_NAME]
        )

    def directory_holds(self) -> list[str]:
        return sorted(entry.name for entry in self.token_file.parent.iterdir())

    def directory_holds_login_and_sibling(self) -> list[str]:
        return sorted([self.token_file.name, self.sibling.name])

    def trees(self) -> list[Path]:
        return sorted(self.root.iterdir())

    def retained_login(self) -> bytes:
        (tree,) = self.trees()
        return (tree / RUN_HOME_DIRECTORY_NAME / AUTH_FILE_NAME).read_bytes()


def an_arrangement(
    tmp_path: Path,
    source: bytes = DUE_ORIGINAL,
    *,
    binary: Path = UNUSED_BINARY,
    entry: RenewalEntry = CODEX_RENEWAL,
    token_file: Path | None = None,
) -> Arrangement:
    """A login directory holding ``source`` (or the given file as it is) and a private root."""
    if token_file is None:
        directory = tmp_path / "login"
        directory.mkdir()
        token_file = write_login(directory / LOGIN_NAME, source)
    root = a_private_root(tmp_path, "renewal-root")
    clocks = Clocks()
    renewer = ProviderRenewer(
        Provider.OPENAI,
        entry,
        binary=binary,
        token_file=token_file,
        working_root=PrivateWorkingRoot(root),
        wall_clock=lambda: clocks.wall,
        monotonic_clock=lambda: clocks.monotonic,
    )
    return Arrangement(
        renewer, token_file, root, clocks, token_file.read_bytes(), token_file.stat().st_ino
    )


def a_run_outcome(
    reason: CliRunReason = CliRunReason.COMPLETE, *, became_zombie: bool = False
) -> CliRunOutcome:
    return CliRunOutcome(
        started=True,
        spawn_error=None,
        returncode=0,
        stdout="",
        stderr="",
        complete=reason is CliRunReason.COMPLETE,
        became_zombie=became_zombie,
        reason=reason,
    )


class Confirmation(Enum):
    NOW = "the callback fires before the runner returns"
    LATER = "the callback is held until the test fires it"
    NEVER = "the callback never fires"


@dataclass
class FakeRunner:
    """Stands for ``run_cli_bounded``: runs ``on_home`` on the renewal home, then returns."""

    outcome: CliRunOutcome = field(default_factory=a_run_outcome)
    on_home: Callable[[Path], None] | None = None
    confirmation: Confirmation = Confirmation.NOW
    spawn_failed: bool = False
    calls: int = 0
    children: list[Any] = field(default_factory=list)
    _held: list[Callable[[], None]] = field(default_factory=list)

    def __call__(self, child: Any, **options: Any) -> CliRunOutcome:
        self.calls += 1
        self.children.append(child)
        if self.spawn_failed:
            confirm: Callable[[], None] = options["on_spawn_failed"]
        else:
            confirm = _reaping(options["on_reaped"])
        self._held.append(confirm)
        if self.on_home is not None:
            self.on_home(Path(child.environment["CODEX_HOME"]))
        if self.confirmation is Confirmation.NOW:
            self._held.pop()
            confirm()
        return self.outcome

    def confirm_late(self) -> None:
        assert self._held
        self._held.pop()()

    def confirm_whatever_is_held(self) -> None:
        while self._held:
            self._held.pop()()


def _reaping(on_reaped: Callable[[int, bool], None]) -> Callable[[], None]:
    return lambda: on_reaped(1, False)


_RUNNERS: list[FakeRunner] = []


@pytest.fixture(autouse=True)
def the_held_ends_confirmed() -> Iterator[None]:
    """A test that leaves an end unconfirmed must not keep a process-wide pool slot."""
    yield
    for runner in _RUNNERS:
        runner.confirm_whatever_is_held()
    _RUNNERS.clear()


def a_fake_runner(monkeypatch: pytest.MonkeyPatch, **fields: Any) -> FakeRunner:
    runner = FakeRunner(**fields)
    _RUNNERS.append(runner)
    monkeypatch.setattr(renewal, "run_cli_bounded", runner)
    return runner


def writing(document: bytes) -> Callable[[Path], None]:
    """What a renewing CLI did: it left ``document`` as the home's login."""

    def write(home: Path) -> None:
        (home / AUTH_FILE_NAME).write_bytes(document)

    return write


def removing_the_login(home: Path) -> None:
    (home / AUTH_FILE_NAME).unlink()


def linking_the_login(home: Path) -> None:
    (home / AUTH_FILE_NAME).unlink()
    (home / AUTH_FILE_NAME).symlink_to(home / "config.toml")


def oversizing_the_login(home: Path) -> None:
    (home / AUTH_FILE_NAME).write_bytes(b"x" * (MAX_SECRET_FILE_BYTES + 1))


def _taken_pool_slots() -> list[Any]:
    """Take every free Codex pool slot."""
    slots = []
    while (slot := CODEX_CLI_PROCESS_POOL.reserve()) is not None:
        slots.append(slot)
    return slots


@contextmanager
def pool_filled() -> Iterator[None]:
    """Every Codex pool slot is held, those already held elsewhere included."""
    slots = _taken_pool_slots()
    try:
        yield
    finally:
        for slot in slots:
            slot.release()


def held_pool_slots() -> int:
    """How many Codex pool slots are held now; compare it with the count before an attempt."""
    slots = _taken_pool_slots()
    for slot in slots:
        slot.release()
    return CODEX_CLI_PROCESS_POOL.limit - len(slots)


def logged_once(
    caplog: pytest.LogCaptureFixture, produce: Callable[[], RenewalReport | None]
) -> RenewalReport:
    """The report ``produce`` returns, after checking that it logged exactly one record for it."""
    caplog.set_level(logging.DEBUG, logger=renewal.log.name)
    before = len(_records(caplog))
    report = produce()
    assert report is not None
    (record,) = _records(caplog)[before:]
    expected_level = {
        renewal.RenewalOutcome.NOT_DUE: logging.DEBUG,
        renewal.RenewalOutcome.RENEWED: logging.INFO,
        renewal.RenewalOutcome.INTERRUPTED: logging.ERROR,
    }.get(report.outcome, logging.WARNING)
    text = record.getMessage()
    assert record.levelno == expected_level
    assert text.startswith(f"{report.provider.value} login renewal: {report.outcome.value} (")
    if report.cause is not None:
        assert report.cause.value in text
    return report


def logged_outcomes(caplog: pytest.LogCaptureFixture) -> list[renewal.RenewalOutcome]:
    """The outcome of each report the renewer logged, in order; the level must allow DEBUG."""
    return [
        next(
            outcome
            for outcome in renewal.RenewalOutcome
            if f"login renewal: {outcome.value} (" in record.getMessage()
        )
        for record in _records(caplog)
    ]


def logged_levels(caplog: pytest.LogCaptureFixture) -> list[int]:
    return [record.levelno for record in _records(caplog)]


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == renewal.log.name]


def the_secrets_of(*documents: bytes) -> tuple[str, ...]:
    """Every token of the login documents, as a canary that no output may carry."""
    return tuple(
        value
        for document in documents
        for value in json.loads(document)["tokens"].values()
        if isinstance(value, str) and value
    )


class BlockedHook:
    """An ``on_home`` hook that reports it was entered, then blocks until it is released."""

    def __init__(self, then: Callable[[Path], None] | None = None) -> None:
        self.entered = threading.Event()
        self._released = threading.Event()
        self._then = then

    def __call__(self, home: Path) -> None:
        self.entered.set()
        assert self._released.wait(TEST_WAIT_SECONDS), "the blocked renewal was never released"
        if self._then is not None:
            self._then(home)

    def release(self) -> None:
        self._released.set()

    async def reached(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, TEST_WAIT_SECONDS)


class RecordingPause:
    """The schedule's pause: records the requested seconds and stops the schedule after N waits."""

    def __init__(self, stops_after: int) -> None:
        self._stops_after = stops_after
        self.seconds: list[float] = []
        self.exhausted = asyncio.Event()

    async def __call__(self, seconds: float, stopping: asyncio.Event) -> None:
        self.seconds.append(seconds)
        if len(self.seconds) >= self._stops_after:
            stopping.set()
            self.exhausted.set()


@dataclass(frozen=True)
class CodexServiceFiles:
    """A service configuration file on the Codex table, with the login it renews."""

    configuration: Path
    login: Path
    login_document: bytes
    root: Path
    ran: Path
    """Exists once the stand-in has been run past ``--version``."""
    pids: Path
    """One line per run of the stand-in: the pid of its shell, the leader of its own group."""

    def started_pids(self) -> list[int]:
        return [int(line) for line in self.pids.read_text().split()] if self.pids.exists() else []

    def traces(self) -> tuple[bool, bytes, bool, bool]:
        """In order: a child ran, the login's bytes, a sibling exists, the root holds anything."""
        return (
            self.ran.exists(),
            self.login.read_bytes(),
            swap_sibling(self.login).exists(),
            any(self.root.iterdir()),
        )

    def traces_without_a_renewal(self) -> tuple[bool, bytes, bool, bool]:
        """No child ran, the login is as it was, and nothing sits beside it or under the root."""
        return (False, self.login_document, False, False)


def codex_service_files(
    directory: Path,
    login_document: bytes,
    renewed: bytes,
    *,
    then: str = "exit 0",
    port: int | None = None,
) -> CodexServiceFiles:
    """The service's TOML for a Codex stand-in that renews the login into ``renewed``."""
    fake_directory = directory / "fake-codex"
    fake_directory.mkdir()
    ran = fake_directory / "ran"
    pids = fake_directory / "pids"
    binary = a_renewing_fake_codex(
        fake_directory,
        renewed,
        f": > {shlex.quote(str(ran))}\n{then}",
        version_line=CODEX_TABLE.version_line,
        first=f"echo $$ >> {shlex.quote(str(pids))}\n",
    )
    login = write_login(fake_directory / CODEX_TABLE.login_name, login_document)
    document = CODEX_TABLE.document(directory, binary, login)
    if port is not None:
        document["server"] = {"host": "127.0.0.1", "port": port}
    configuration = directory / "agent-runtime-service.toml"
    configuration.write_text("\n".join(_toml_lines(document)) + "\n", encoding="utf-8")
    return CodexServiceFiles(
        configuration, login, login_document, Path(document["cli"]["working_root"]), ran, pids
    )
