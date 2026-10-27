"""``agent-runtime-service-login``: installs a provider login from standard input and checks the
file with the service's own readers (matrix cells 12 to 17: interrupts, stderr, ``--check``,
refusals before the read).
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from provider_test_support import a_codex_login
from service_login_support import (  # noqa: F401 - the fixtures are named in their decorators
    CANARY_FOREIGN,
    CANARY_REFRESH,
    CODEX_FILE_REFUSAL,
    GOOD_DOCUMENTS,
    LOGIN_NAME,
    NEW_CLAUDE_TOKEN,
    RUN_THE_COMMAND,
    Installation,
    Said,
    Stream,
    a_login_installation,
    an_installation,
    an_old_file_fails,
    assert_not_written,
    line_of,
    reader_hook,
    run,
    send,
    send_while_blocked,
    the_signal_handlers_restored,
    the_signals_received,
    what_the_writer_returned,
    what_was_said,
)
from test_service_codex_profile import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
    write_login,
)

import agent_runtime_service.config as service_config
import agent_runtime_service.login as login
import agent_runtime_service.process_hardening as hardening
from agent_runtime_service.cli import main as service_main
from agent_runtime_service.config import Provider, load_config
from agent_runtime_service.login import main
from agent_runtime_service.login_file import SwapRefused


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
    before = installation.snapshot()

    status, heard = run(installation, stream, said, "--check")

    assert (status, stream.was_read) == (0, False)
    assert installation.snapshot() == before
    assert before[2] == [LOGIN_NAME]
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
    stream = Stream(b"never read")
    before = installation.snapshot()

    status, heard = run(installation, stream, said, "--check")

    assert (status, stream.was_read) == (1, False)
    assert installation.snapshot() == before
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


# The command's other refusals and the signals' reach: matrix cells 12 and 16.


def test_standard_input_that_does_not_exist_is_a_closed_refusal(
    installation: Installation, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", None)

    with said.watching():
        status = main(installation.command())

    assert status == 1
    assert "not written: standard input cannot be read" in said.descriptor
    assert installation.unchanged()
    assert installation.directory_holds() == [LOGIN_NAME]


def test_a_login_file_without_a_file_name_is_refused_before_anything_is_read(
    installation: Installation, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_load_config = service_config.load_config

    def rooted(path: Path, **keywords: Any) -> Any:
        loaded = real_load_config(path, **keywords)
        profile = loaded.profiles[installation.profile].model_copy(update={"token_file": Path("/")})
        return loaded.model_copy(update={"profiles": {installation.profile: profile}})

    monkeypatch.setattr(service_config, "load_config", rooted)
    stream = Stream(GOOD_DOCUMENTS[installation.provider])

    status, heard = run(installation, stream, said)

    assert (status, stream.was_read) == (2, False)
    assert "has no file name" in heard
    assert installation.unchanged()


@pytest.mark.parametrize("interrupt", login.INTERRUPT_SIGNALS, ids=lambda number: number.name)
def test_a_process_that_installed_a_login_exits_zero_whatever_signal_comes_after_the_commit(
    tmp_path: Path, interrupt: signal.Signals
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX)
    document = GOOD_DOCUMENTS[Provider.CODEX]
    after_the_commit = (
        "import os, signal, sys; from agent_runtime_service.login import main;"
        f" status = main(); os.kill(os.getpid(), signal.{interrupt.name}); sys.exit(status)"
    )

    finished = subprocess.run(
        [sys.executable, "-c", after_the_commit, *installation.command()],
        input=document,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert finished.returncode == 0
    assert installation.login.read_bytes() == document


@pytest.mark.parametrize("where", ["check", "pre-check"])
def test_interrupts_before_the_read_are_disregarded(
    installation: Installation,
    said: Said,
    monkeypatch: pytest.MonkeyPatch,
    recorded_signals: Callable[[], list[int]],
    where: str,
) -> None:
    extra: tuple[str, ...] = ()
    if where == "pre-check":
        real_lstat = os.lstat

        def inspecting(path: Any, *rest: Any) -> os.stat_result:
            if Path(path) == installation.sibling:
                send(*login.INTERRUPT_SIGNALS)
            return real_lstat(path, *rest)

        monkeypatch.setattr(os, "lstat", inspecting)
    else:
        reader_hook(monkeypatch, installation.provider, lambda: send(*login.INTERRUPT_SIGNALS))
        extra = ("--check",) if where == "check" else ()
    stream = Stream(GOOD_DOCUMENTS[installation.provider])

    status, _ = run(installation, stream, said, *extra)

    assert status == 0
    assert recorded_signals() == []
