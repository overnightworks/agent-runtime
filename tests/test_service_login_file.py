"""The login writer through the command: ``swap_in_login_file`` swaps a checked document in beside
the old file and removes its own sibling, or says why not (matrix cells 1 to 11).
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    SAMPLE_CLAUDE_SETUP_TOKEN,
)
from service_login_support import (  # noqa: F401 - the fixtures are named in their decorators
    CANARY_FOREIGN,
    CANARY_REFRESH,
    CODEX_FILE_REFUSAL,
    GOOD_DOCUMENTS,
    LOGIN_NAME,
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
from service_support import reaches_text

import agent_runtime_service.config as service_config
import agent_runtime_service.login_file as login_file
from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_service.config import Provider
from agent_runtime_service.login_file import SwapRefused, swap_in_login_file

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
        "holds a JSON document",
        id="claude-credentials-file",
    ),
    pytest.param(
        Provider.CLAUDE,
        b"sk-ant-api03-" + CANARY_FOREIGN.encode() + b"\n",
        (CANARY_FOREIGN,),
        "holds an API key",
        id="claude-api-key",
    ),
    pytest.param(
        Provider.CLAUDE, b" new-token\n", (), "holds whitespace", id="claude-leading-space"
    ),
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
    assert f"the new document for {installation.login} was refused" in heard
    assert str(installation.sibling) not in heard
    assert stream.tell() == min(len(document), MAX_SECRET_FILE_BYTES + 1)

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


def a_sibling_not_confirmed_gone(monkeypatch: pytest.MonkeyPatch, sibling: Path, how: str) -> None:
    """The writer's unlink and lstat of ``sibling`` misbehave as ``how`` says.

    ``unlink-raises`` and ``unlink-does-nothing`` leave the file for a truthful lstat to find;
    ``lstat-denied`` lets the unlink succeed and then refuses every inspection; ``both`` fails
    both. The check before the read still passes, because no unlink has happened yet.
    """
    real_unlink, real_lstat = os.unlink, os.lstat
    unlinks: list[Path] = []

    def unlinking(path: Any, *rest: Any) -> None:
        if Path(path) == sibling:
            unlinks.append(sibling)
            if how in ("unlink-raises", "both"):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
            if how == "unlink-does-nothing":
                return
        real_unlink(path, *rest)

    def inspecting(path: Any, *rest: Any) -> os.stat_result:
        if Path(path) == sibling and unlinks and how in ("lstat-denied", "both"):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real_lstat(path, *rest)

    monkeypatch.setattr(os, "unlink", unlinking)
    monkeypatch.setattr(os, "lstat", inspecting)


@pytest.mark.parametrize(
    "how", ["unlink-raises", "unlink-does-nothing", "lstat-denied", "both"]
)
def test_a_sibling_that_cannot_be_confirmed_gone_ends_with_exit_three_naming_it(
    tmp_path: Path, said: Said, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    installation = an_installation(tmp_path, Provider.CODEX)
    a_sibling_not_confirmed_gone(monkeypatch, installation.sibling, how)

    status, heard = run(installation, Stream(b""), said)

    assert status == 3
    assert installation.unchanged()
    assert f"{installation.sibling} is not confirmed gone and may hold all or part" in heard
    assert "not written" in heard


# Atomic replacement: matrix cell 2.


def test_the_old_file_is_whole_until_the_rename_replaces_it(
    installation: Installation, said: Said, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    before_rename: list[tuple[bytes, int]] = []

    def replacing(source: Any, destination: Any) -> None:
        before_rename.append((installation.login.read_bytes(), installation.login.stat().st_ino))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replacing)

    status, _ = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert status == 0
    assert before_rename == [(installation.old_bytes, installation.old_inode)]


def test_a_rename_that_fails_leaves_the_old_file_with_its_bytes_and_inode(
    installation: Installation,
    said: Said,
    refusals: list[SwapRefused | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(source: Any, destination: Any) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "replace", failing)

    status, heard = run(installation, Stream(GOOD_DOCUMENTS[installation.provider]), said)

    assert_not_written(
        installation, status, heard, refusals, (CANARY_REFRESH,), "Input/output error"
    )


# A path with no file name has no sibling to swap through.


def test_the_writer_refuses_a_path_without_a_file_name_as_a_closed_result() -> None:
    document = GOOD_DOCUMENTS[Provider.CODEX]

    refused = swap_in_login_file(Path("/"), document, lambda path: None)

    assert refused == SwapRefused(refused.reason, False)
    assert "has no file name" in refused.reason
    assert not reaches_text(refused, (CANARY_REFRESH,))
