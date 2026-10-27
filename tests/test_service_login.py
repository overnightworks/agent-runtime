"""``agent-runtime-service-login``: installs a provider login from standard input, through the one
writer, and checks the file with the service's own readers.

Test values: a Codex profile ``codex-main`` and a Claude profile ``subscription``, each logging
in from ``<tmp>/login/<file>``; the canaries below stand for secrets that no line or held
exception may carry.
"""

from __future__ import annotations

import errno
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    SAMPLE_CLAUDE_SETUP_TOKEN,
    a_codex_login,
    write_claude_token_file,
)
from service_support import CLI_PROFILE, TEST_ISSUER, _service_config_document, reaches_text
from test_service_codex_profile import (
    CODEX_PROFILE,
    a_service_fake,
    codex_document,
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
    write_login,
    written,
)

import agent_runtime_service.config as service_config
import agent_runtime_service.login as login
import agent_runtime_service.login_file as login_file
import agent_runtime_service.process_hardening as hardening
from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_service.cli import main as service_main
from agent_runtime_service.config import Provider, load_config
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
    """A configuration whose profile logs in from ``login`` in a directory of its own."""

    config: Path
    profile: str
    provider: Provider
    login: Path
    old_bytes: bytes
    old_inode: int

    @property
    def sibling(self) -> Path:
        return swap_sibling(self.login)

    def command(self, *extra: str) -> list[str]:
        return ["--config", str(self.config), "--profile", self.profile, *extra]

    def unchanged(self) -> bool:
        status = self.login.stat()
        return (self.login.read_bytes(), status.st_ino) == (self.old_bytes, self.old_inode)

    def directory_holds(self) -> list[str]:
        return sorted(entry.name for entry in self.login.parent.iterdir())


def an_installation(tmp_path: Path, provider: Provider, *, old: bool = True) -> Installation:
    directory = tmp_path / "login"
    directory.mkdir()
    path = directory / LOGIN_NAME
    if provider is Provider.CODEX:
        if old:
            write_login(path, a_codex_login(tokens={"access_token": "old-access-token"}))
        document = codex_document(tmp_path, a_service_fake(tmp_path), path)
        profile = CODEX_PROFILE
    else:
        if old:
            write_claude_token_file(path, OLD_CLAUDE_TOKEN.strip())
        document = _service_config_document(TEST_ISSUER, None, None, token_file=path)
        profile = CLI_PROFILE
    config = written(tmp_path, document)
    status = path.stat() if old else None
    return Installation(
        config,
        profile,
        provider,
        path,
        path.read_bytes() if old else b"",
        status.st_ino if status else 0,
    )


@pytest.fixture(params=[Provider.CODEX, Provider.CLAUDE], ids=["codex", "claude"])
def installation(request: pytest.FixtureRequest, tmp_path: Path) -> Installation:
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


@pytest.fixture
def said(capfd: pytest.CaptureFixture[str]) -> Said:
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


@pytest.fixture
def refusals(monkeypatch: pytest.MonkeyPatch) -> list[SwapRefused | None]:
    """What the writer returned at each call, for the check that none holds a canary."""
    returned: list[SwapRefused | None] = []

    def recording(*arguments: Any) -> SwapRefused | None:
        returned.append(swap_in_login_file(*arguments))
        return returned[-1]

    monkeypatch.setattr(login, "swap_in_login_file", recording)
    return returned


@pytest.fixture
def recorded_signals() -> Iterator[Callable[[], list[int]]]:
    """Recording handlers for the three signals; the command must leave them in place."""
    received: list[int] = []

    def record(number: int, frame: object) -> None:
        received.append(number)

    previous = {number: signal.signal(number, record) for number in login.INTERRUPT_SIGNALS}
    yield lambda: received
    for number in login.INTERRUPT_SIGNALS:
        assert signal.getsignal(number) is record
    for number, handler in previous.items():
        signal.signal(number, handler)


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
    return f"{LINE_PREFIX}profiles.{installation.profile}.token_file: {text}"


# Install, the good path: matrix cells 1 and 2.


def test_a_login_is_written_exactly_and_reported_on_descriptor_two_only(
    installation: Installation, said: Said
) -> None:
    document = GOOD_DOCUMENTS[installation.provider]

    status, heard = run(installation, Stream(document), said)

    assert (status, installation.login.read_bytes()) == (0, document)
    assert stat.S_IMODE(installation.login.stat().st_mode) == 0o600
    assert installation.directory_holds() == [LOGIN_NAME]
    assert heard == line_of(installation, "written") + "\n"
    assert said.stderr == ""
    assert not any(word in heard for word in (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN, CANARY_REFRESH))


def test_a_usable_file_is_replaced_by_one_rename_and_the_reader_reads_the_new_document(
    installation: Installation, said: Said
) -> None:
    status, _ = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    reader = service_config.LOGIN_READERS[installation.provider]
    assert status == 0
    assert installation.login.stat().st_ino != installation.old_inode
    assert reader(installation.login) is not None
    assert installation.login.read_bytes() != installation.old_bytes


# Not written: matrix cells 3 to 8. The group invariant is `assert_not_written`.


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


REFUSED_DOCUMENTS = [
    pytest.param(Provider.CODEX, b"", (), "is not a Codex login document", id="codex-empty"),
    pytest.param(
        Provider.CODEX,
        json.dumps({"OPENAI_API_KEY": CANARY_FOREIGN}).encode(),
        (CANARY_FOREIGN,),
        CODEX_FILE_REFUSAL,
        id="codex-api-key",
    ),
    pytest.param(
        Provider.CODEX,
        b"not json " + CANARY_FOREIGN.encode(),
        (CANARY_FOREIGN,),
        "is not a Codex login document",
        id="codex-not-json",
    ),
    pytest.param(
        Provider.CODEX,
        b"x" * (MAX_SECRET_FILE_BYTES + 1000),
        (),
        f"is larger than {MAX_SECRET_FILE_BYTES} bytes",
        id="codex-too-large",
    ),
    pytest.param(
        Provider.CODEX,
        SAMPLE_CLAUDE_SETUP_TOKEN.encode(),
        (SAMPLE_CLAUDE_SETUP_TOKEN,),
        "is not a Codex login document",
        id="codex-given-a-claude-token",
    ),
    pytest.param(
        Provider.CLAUDE,
        b'{"claudeAiOauth": {"accessToken": "' + CANARY_FOREIGN.encode() + b'"}}',
        (CANARY_FOREIGN, "claudeAiOauth"),
        "",
        id="claude-credentials-file",
    ),
    pytest.param(
        Provider.CLAUDE,
        b"sk-ant-api03-" + CANARY_FOREIGN.encode() + b"\n",
        (CANARY_FOREIGN,),
        "",
        id="claude-api-key",
    ),
    pytest.param(Provider.CLAUDE, b" new-token\n", (), "", id="claude-leading-space"),
]


@pytest.mark.parametrize(("provider", "document", "canaries", "reason"), REFUSED_DOCUMENTS)
def test_a_document_the_profiles_reader_refuses_is_not_written_and_the_old_file_stays(
    tmp_path: Path,
    said: Said,
    refusals: list[SwapRefused | None],
    provider: Provider,
    document: bytes,
    canaries: tuple[str, ...],
    reason: str,
) -> None:
    installation = an_installation(tmp_path, provider)
    stream = Stream(document)

    status, heard = run(installation, stream, said)

    assert_not_written(installation, status, heard, refusals, canaries, reason)
    assert stream.tell() == min(len(document), MAX_SECRET_FILE_BYTES + 1)


@pytest.mark.parametrize("interrupt", login.INTERRUPT_SIGNALS, ids=lambda number: number.name)
def test_an_interrupt_while_reading_ends_the_read_and_a_second_one_is_disregarded(
    installation: Installation,
    said: Said,
    refusals: list[SwapRefused | None],
    recorded_signals: Callable[[], list[int]],
    monkeypatch: pytest.MonkeyPatch,
    interrupt: signal.Signals,
) -> None:
    other = next(number for number in login.INTERRUPT_SIGNALS if number != interrupt)
    handlers_when_reported: list[object] = []
    say = login._say

    def reporting(line: str) -> None:
        handlers_when_reported.extend(signal.getsignal(n) for n in login.INTERRUPT_SIGNALS)
        say(line)

    monkeypatch.setattr(login, "_say", reporting)

    status, heard = run(
        installation,
        Stream(GOOD_DOCUMENTS[installation.provider], lambda: send_while_blocked(interrupt, other)),
        said,
    )

    assert_not_written(
        installation, status, heard, refusals, (CANARY_REFRESH,), "interrupted while reading"
    )
    assert recorded_signals() == []
    assert set(handlers_when_reported) == {login._disregard}


def test_a_login_directory_that_cannot_be_written_refuses_the_create(
    installation: Installation, said: Said, refusals: list[SwapRefused | None]
) -> None:
    installation.login.parent.chmod(0o500)
    try:
        status, heard = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)
    finally:
        installation.login.parent.chmod(0o700)

    assert_not_written(
        installation, status, heard, refusals, (CANARY_REFRESH,), f"{installation.sibling}"
    )
    assert "cannot be created: Permission denied" in heard


def test_a_full_disk_while_writing_removes_the_sibling_and_names_the_cause(
    installation: Installation,
    said: Said,
    refusals: list[SwapRefused | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FullDisk:
        def __init__(self, descriptor: int, mode: str) -> None:
            self.file = open(descriptor, mode)  # noqa: SIM115 - closed by __exit__

        def __enter__(self) -> FullDisk:
            return self

        def __exit__(self, *exception: object) -> None:
            self.file.close()

        def write(self, content: bytes) -> None:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(login_file, "open", FullDisk, raising=False)

    status, heard = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert_not_written(
        installation,
        status,
        heard,
        refusals,
        (CANARY_REFRESH,),
        f"{installation.login} cannot be swapped in: No space left on device",
    )


def test_a_path_the_rename_cannot_replace_removes_the_sibling_and_leaves_the_directory(
    tmp_path: Path, said: Said, refusals: list[SwapRefused | None]
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX, old=False)
    installation.login.mkdir()
    (installation.login / "kept").write_text("kept")

    status, heard = run(installation, Stream(GOOD_DOCUMENTS[Provider.CODEX]), said)

    assert status == 1
    assert installation.directory_holds() == [LOGIN_NAME]
    assert (installation.login / "kept").read_text() == "kept"
    assert f"{installation.login} cannot be swapped in:" in heard
    assert CANARY_REFRESH not in heard
    assert not reaches_text(refusals, (CANARY_REFRESH,))


def a_frame_that_holds_the_document_raises(
    where: str, installation: Installation, monkeypatch: pytest.MonkeyPatch
) -> Stream:
    def raising() -> None:
        error = RuntimeError(CANARY_FOREIGN)
        error.detail = CANARY_FOREIGN  # type: ignore[attr-defined]
        raise error

    if where == "read":
        return Stream(GOOD_DOCUMENTS[installation.provider], raising)
    reader_hook(monkeypatch, installation.provider, raising)
    return Stream(GOOD_DOCUMENTS[installation.provider])


@pytest.mark.parametrize("where", ["read", "reader"])
def test_an_unexpected_exception_in_a_frame_that_holds_the_document_is_a_closed_reason(
    installation: Installation,
    said: Said,
    refusals: list[SwapRefused | None],
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    stream = a_frame_that_holds_the_document_raises(where, installation, monkeypatch)

    status, heard = run(installation, stream, said)

    assert_not_written(
        installation,
        status,
        heard,
        refusals,
        (CANARY_FOREIGN, CANARY_REFRESH),
        "failed: RuntimeError",
    )


# The sibling and the commit point: matrix cells 9 to 12.


def test_a_leftover_sibling_stops_the_command_before_it_reads_anything(
    installation: Installation, said: Said
) -> None:
    installation.sibling.write_bytes(b"left by an earlier install")
    stream = Stream(GOOD_DOCUMENTS[installation.provider])

    status, heard = run(installation, stream, said)

    assert (status, stream.was_read) == (2, False)
    assert f"{installation.sibling} exists from an earlier install" in heard
    assert installation.unchanged()
    assert installation.sibling.read_bytes() == b"left by an earlier install"


def test_a_sibling_that_appears_after_the_check_is_refused_and_left_untouched(
    installation: Installation, said: Said
) -> None:
    def foreign_sibling_appears() -> None:
        installation.sibling.write_bytes(b"foreign")

    status, heard = run(
        installation,
        Stream(GOOD_DOCUMENTS[installation.provider], foreign_sibling_appears),
        said,
    )

    assert status == 1
    assert "cannot be created: File exists" in heard
    assert installation.unchanged()
    assert installation.sibling.read_bytes() == b"foreign"


@pytest.mark.parametrize("failing", [["unlink"], ["unlink", "lstat"]], ids=["unlink", "both"])
def test_a_sibling_that_cannot_be_confirmed_gone_ends_with_exit_three_naming_it(
    tmp_path: Path, said: Said, monkeypatch: pytest.MonkeyPatch, failing: list[str]
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX)
    real_lstat = os.lstat
    for name in failing:
        if name == "lstat":
            # only once the sibling exists, so that the check before the read still passes
            def inspecting(path: Any, *rest: Any, real: Any = real_lstat) -> Any:
                result = real(path, *rest)
                if Path(path) == installation.sibling:
                    raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
                return result

            monkeypatch.setattr(os, "lstat", inspecting)
        else:
            an_old_file_fails(monkeypatch, name, installation.sibling)

    status, heard = run(installation, Stream(b""), said)

    assert status == 3
    assert installation.unchanged()
    assert f"{installation.sibling} is not confirmed gone and may hold all or part" in heard
    assert "not written" in heard


def test_a_leftover_check_that_cannot_inspect_the_sibling_refuses_before_reading(
    tmp_path: Path, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX)
    an_old_file_fails(monkeypatch, "lstat", installation.sibling)
    stream = Stream(GOOD_DOCUMENTS[Provider.CODEX])

    status, _ = run(installation, stream, said)

    assert (status, stream.was_read, installation.unchanged()) == (2, False, True)


def a_commit_with_every_interrupt_arriving(
    where: str, monkeypatch: pytest.MonkeyPatch, installation: Installation
) -> None:
    def everything() -> None:
        send(*login.INTERRUPT_SIGNALS)

    if where == "swap":
        reader_hook(monkeypatch, installation.provider, everything)
        return
    real_replace = os.replace

    def replacing(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        everything()

    monkeypatch.setattr(os, "replace", replacing)


@pytest.mark.parametrize("where", ["swap", "after-replace"])
def test_interrupts_during_the_swap_and_after_the_commit_are_disregarded(
    installation: Installation,
    said: Said,
    monkeypatch: pytest.MonkeyPatch,
    recorded_signals: Callable[[], list[int]],
    where: str,
) -> None:
    a_commit_with_every_interrupt_arriving(where, monkeypatch, installation)
    document = GOOD_DOCUMENTS[installation.provider]

    status, heard = run(installation, Stream(document), said)

    assert (status, installation.login.read_bytes()) == (0, document)
    assert heard == line_of(installation, "written") + "\n"
    assert recorded_signals() == []


# The status survives a broken stderr: matrix cell 13.


def a_broken_descriptor_two(monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write

    def writing(descriptor: int, data: Any) -> int:
        if descriptor == 2:
            raise BrokenPipeError(errno.EPIPE, os.strerror(errno.EPIPE))
        return real_write(descriptor, data)

    monkeypatch.setattr(os, "write", writing)


def test_a_broken_descriptor_two_changes_neither_the_status_nor_the_written_file(
    installation: Installation, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_broken_descriptor_two(monkeypatch)
    document = GOOD_DOCUMENTS[installation.provider]

    status = main(installation.command(), Stream(document))

    assert (status, installation.login.read_bytes()) == (0, document)


def test_a_broken_descriptor_two_keeps_exit_three_and_the_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX)
    an_old_file_fails(monkeypatch, "unlink", installation.sibling)
    a_broken_descriptor_two(monkeypatch)

    status = main(installation.command(), Stream(b""))

    assert (status, installation.sibling.exists()) == (3, True)


@pytest.mark.parametrize(
    ("document", "expected"),
    [(GOOD_DOCUMENTS[Provider.CODEX], 0), (b"", 1)],
    ids=["usable", "refused"],
)
def test_a_process_whose_stderr_is_a_closed_pipe_exits_with_the_installs_status(
    tmp_path: Path, document: bytes, expected: int
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        finished = subprocess.run(
            [sys.executable, "-c", RUN_THE_COMMAND, *installation.command()],
            input=document,
            stdout=subprocess.PIPE,
            stderr=write_end,
            check=False,
        )
    finally:
        os.close(write_end)

    assert finished.returncode == expected
    assert (installation.login.read_bytes() == document) is (expected == 0)


# --check: matrix cells 14 and 15.


@pytest.mark.parametrize(
    "login_bytes",
    [
        a_codex_login(),
        a_codex_login(last_refresh="2001-01-01T00:00:00Z"),
        NEW_CLAUDE_TOKEN,
    ],
    ids=["codex", "codex-long-since-refreshed", "claude"],
)
def test_check_accepts_a_file_the_reader_accepts_without_reading_input_or_writing(
    tmp_path: Path, said: Said, login_bytes: bytes
) -> None:
    provider = Provider.CLAUDE if login_bytes == NEW_CLAUDE_TOKEN else Provider.CODEX
    installation = an_installation(tmp_path, provider)
    write_login(installation.login, login_bytes)
    stream = Stream(b"never read")

    status, heard = run(installation, stream, said, "--check")

    assert (status, stream.was_read, installation.directory_holds()) == (0, False, [LOGIN_NAME])
    assert "accepted by the service's reader" in heard
    assert "shows only in a run" in heard


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        pytest.param(None, "cannot be read: No such file or directory", id="missing"),
        pytest.param(
            json.dumps({"OPENAI_API_KEY": CANARY_FOREIGN}).encode(),
            CODEX_FILE_REFUSAL,
            id="api-key",
        ),
    ],
)
def test_check_refuses_a_missing_or_refused_file_with_the_readers_reason(
    tmp_path: Path, said: Said, contents: bytes | None, reason: str
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX, old=False)
    if contents is not None:
        write_login(installation.login, contents)

    status, heard = run(installation, Stream(b""), said, "--check")

    assert status == 1
    assert f"{installation.login}" in heard
    assert reason in heard
    assert CANARY_FOREIGN not in heard


# Refused before reading: matrix cell 16.


def test_an_unknown_profile_is_refused_before_anything_is_read(
    installation: Installation, said: Said
) -> None:
    stream = Stream(GOOD_DOCUMENTS[installation.provider])

    with said.watching():
        status = main(["--config", str(installation.config), "--profile", "nobody"], stream)

    assert (status, stream.was_read, installation.unchanged()) == (2, False, True)
    assert "profiles.nobody: no such profile" in said.descriptor


@pytest.mark.parametrize("extra", [[], ["--check"]], ids=["install", "check"])
def test_a_configuration_the_service_refuses_is_refused_with_its_field_text(
    tmp_path: Path, said: Said, extra: list[str]
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX)
    text = installation.config.read_text().replace("jwks_url", "jwks_address")
    installation.config.write_text(text)
    stream = Stream(GOOD_DOCUMENTS[Provider.CODEX])

    status, heard = run(installation, stream, said, *extra)

    assert (status, stream.was_read, installation.unchanged()) == (2, False, True)
    assert "identity.jwks_address" in heard or "identity.jwks_url" in heard


def test_hardening_under_root_refuses_before_the_configuration_is_read(
    installation: Installation, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hardening, "effective_user_id", lambda: 0)
    installation.config.unlink()
    stream = Stream(GOOD_DOCUMENTS[installation.provider])

    with said.watching():
        status = main(installation.command(), stream)

    assert (status, stream.was_read, installation.unchanged()) == (2, False, True)
    assert "running as root" in said.descriptor


# The bypass of a bad file is the command's alone: matrix cell 17.


@pytest.mark.parametrize(
    "present",
    [None, json.dumps({"OPENAI_API_KEY": CANARY_FOREIGN}).encode()],
    ids=["missing", "refused"],
)
def test_the_service_refuses_a_bad_login_at_start_and_only_the_command_repairs_it(
    tmp_path: Path, said: Said, present: bytes | None
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX, old=False)
    if present is not None:
        write_login(installation.login, present)

    with said.watching():
        refused_at_start = service_main(["--config", str(installation.config)])
    stops = said.stderr
    installed = main(installation.command(), Stream(GOOD_DOCUMENTS[Provider.CODEX]))
    checked = main(installation.command("--check"), Stream(b""))

    assert refused_at_start == 2
    assert f"profiles.{installation.profile}.token_file" in stops
    assert (installed, checked) == (0, 0)
    assert load_config(installation.config).profiles[installation.profile].token_file is not None
