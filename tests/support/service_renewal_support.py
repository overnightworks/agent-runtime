"""Shared arrangement of the renewal attempt's tests: logins with real timing, a renewer under a
temporary directory with hand-moved clocks, and a stand-in for the bounded runner that runs a hook
on the renewal home, returns a chosen outcome and fires the end confirmation now, later or never.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from tests.support.codex_support import a_codex_access_token
from tests.support.provider_test_support import a_codex_login, a_private_root
from tests.support.service_table_support import write_login

from agent_runtime.codex.run import CODEX_CLI_PROCESS_POOL
from agent_runtime.config import PrivateWorkingRoot
from agent_runtime.process import CliRunOutcome, CliRunReason
from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_service import renewal
from agent_runtime_service.config import Provider
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
LOGIN_NAME = "auth.json"
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
        return self.token_file.with_name(f".{self.token_file.name}.new")

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

    def trees(self) -> list[Path]:
        return sorted(self.root.iterdir())

    def retained_login(self) -> bytes:
        (tree,) = self.trees()
        return (tree / "home" / LOGIN_NAME).read_bytes()


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
    _held: Callable[[], None] | None = None

    def __call__(self, child: Any, **options: Any) -> CliRunOutcome:
        self.calls += 1
        self.children.append(child)
        if self.on_home is not None:
            self.on_home(Path(child.environment["CODEX_HOME"]))
        if self.spawn_failed:
            confirm: Callable[[], None] = options["on_spawn_failed"]
        else:
            confirm = _reaping(options["on_reaped"])
        if self.confirmation is Confirmation.NOW:
            confirm()
        else:
            self._held = confirm
        return self.outcome

    def confirm_late(self) -> None:
        assert self._held is not None
        self._held()

    def confirm_whatever_is_held(self) -> None:
        if self._held is not None:
            self._held()


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
        (home / LOGIN_NAME).write_bytes(document)

    return write


def removing_the_login(home: Path) -> None:
    (home / LOGIN_NAME).unlink()


def linking_the_login(home: Path) -> None:
    (home / LOGIN_NAME).unlink()
    (home / LOGIN_NAME).symlink_to(home / "config.toml")


def oversizing_the_login(home: Path) -> None:
    (home / LOGIN_NAME).write_bytes(b"x" * (MAX_SECRET_FILE_BYTES + 1))


@contextmanager
def pool_filled() -> Iterator[None]:
    """Every Codex pool slot is held."""
    slots = [CODEX_CLI_PROCESS_POOL.reserve() for _ in range(CODEX_CLI_PROCESS_POOL.limit)]
    try:
        assert all(slots)
        yield
    finally:
        for slot in slots:
            if slot is not None:
                slot.release()


def pool_is_free() -> bool:
    """Every Codex pool slot can be taken, so none is held."""
    slots = [CODEX_CLI_PROCESS_POOL.reserve() for _ in range(CODEX_CLI_PROCESS_POOL.limit)]
    for slot in slots:
        if slot is not None:
            slot.release()
    return all(slots)


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
