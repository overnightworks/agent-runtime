"""``CodexRenewal`` describes one renewal run of the pinned Codex CLI and reads its result back.

Every row builds the description from arguments only, after ``reset_config()``.
"""

from __future__ import annotations

import copy
import json
import os
import pickle
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest
from tests.support.codex_support import a_codex_access_token
from tests.support.provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    a_codex_login,
    a_private_root,
)

from agent_runtime.codex.renewal import CodexRenewal, CopyUnchanged
from agent_runtime.codex.run import (
    CODEX_CLI_PROCESS_POOL,
    CodexRun,
    CodexStreamDecoder,
)
from agent_runtime.codex_login import (
    RenewableCodexLogin,
    assess_codex_login,
    made_due_copy,
    renewable_codex_login,
)
from agent_runtime.config import PrivateWorkingRoot, reset_config
from agent_runtime.process import run_directories
from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES, SecretFileFault
from agent_runtime.spawn import ChildProcessSpecificationError, closed_environment
from agent_runtime_client.wire import TurnMessage, TurnRole

BINARY = Path("/opt/codex/bin/codex")
NOW = 1_800_000_000
REFRESH_CANARY = "canary-refresh-token"
CANARIES = (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN, REFRESH_CANARY)


def a_login(document: bytes | None = None) -> RenewableCodexLogin:
    login = renewable_codex_login(document or a_codex_login())
    assert isinstance(login, RenewableCodexLogin)
    return login


def a_renewal(**changes: Any) -> CodexRenewal:
    fields: dict[str, Any] = {"binary": BINARY, "login": a_login(), "now": NOW}
    return CodexRenewal(**{**fields, **changes})


def a_home_holding(directory: Path, document: bytes) -> Path:
    home = directory / "home"
    home.mkdir()
    (home / "auth.json").write_bytes(document)
    return home


def test_the_run_files_are_the_made_due_copy_the_fixed_system_text_and_the_file_store() -> None:
    reset_config()
    renewal = a_renewal()

    files = renewal.run_files()

    assert sorted(files) == ["auth.json", "config.toml", "system-prompt"]
    assert files["auth.json"] == made_due_copy(renewal.login, NOW)
    assert files["system-prompt"] == b"You answer in one word. You have no tools."


def test_the_settings_are_the_adapter_defaults_plus_the_file_credential_store() -> None:
    renewal = a_renewal()
    ordinary = CodexRun(
        BINARY, assess_codex_login(renewal.login.document), "", (_message(),), model=None,
        settings=(),
    )

    renewal_settings = tomllib.loads(renewal.run_files()["config.toml"].decode())
    ordinary_settings = tomllib.loads(ordinary.run_files()["config.toml"].decode())

    assert renewal_settings == {**ordinary_settings, "cli_auth_credentials_store": "file"}
    assert "cli_auth_credentials_store" not in ordinary_settings


def test_the_argument_list_is_the_codex_runs_with_no_model(tmp_path: Path) -> None:
    home = tmp_path / "home"
    arguments = a_renewal().start(home, tmp_path / "work").child.arguments

    assert arguments == (
        "exec", "--json", "--skip-git-repo-check", "--ignore-rules", "--ephemeral",
        "--strict-config", "-c", f'model_instructions_file="{home}/system-prompt"', "-",
    )


def test_the_environment_is_the_closed_one_plus_the_codex_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    start = a_renewal().start(home, tmp_path / "work")

    assert start.child.environment == {**closed_environment(home), "CODEX_HOME": str(home)}
    assert (start.child.binary, start.child.working_directory) == (BINARY, tmp_path / "work")


def test_the_request_is_one_fixed_user_message_read_from_stdin(tmp_path: Path) -> None:
    renewal = a_renewal()
    start = renewal.start(tmp_path / "home", tmp_path / "work")

    assert start.stdin == b"User: Say ok."
    assert isinstance(start.decoder, CodexStreamDecoder)
    assert renewal.pool is CODEX_CLI_PROCESS_POOL


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"binary": Path("codex")}, id="relative-binary"),
        pytest.param({"login": RenewableCodexLogin(b"not json")}, id="login-not-usable"),
    ],
)
def test_a_renewal_that_could_not_run_is_refused_at_construction(changes: dict[str, Any]) -> None:
    with pytest.raises(ChildProcessSpecificationError):
        a_renewal(**changes)


def test_the_renewed_document_is_what_the_cli_left_in_the_home(tmp_path: Path) -> None:
    renewed = a_codex_login(tokens={"access_token": "renewed", "refresh_token": "rotated"})

    assert a_renewal().renewed_document(a_home_holding(tmp_path, renewed)) == renewed


def test_a_home_without_auth_json_is_missing(tmp_path: Path) -> None:
    assert a_renewal().renewed_document(tmp_path) is SecretFileFault.MISSING


def test_a_linked_auth_json_is_refused_and_its_target_is_not_read(tmp_path: Path) -> None:
    (tmp_path / "target").write_bytes(a_codex_login())
    (tmp_path / "auth.json").symlink_to(tmp_path / "target")

    assert a_renewal().renewed_document(tmp_path) is SecretFileFault.LINK


@pytest.mark.parametrize("make", [os.mkfifo, os.mkdir], ids=["fifo", "directory"])
def test_an_auth_json_that_is_not_a_file_is_refused(tmp_path: Path, make) -> None:
    make(tmp_path / "auth.json")

    assert a_renewal().renewed_document(tmp_path) is SecretFileFault.NOT_REGULAR


@pytest.mark.parametrize(
    ("size", "fits"), [(MAX_SECRET_FILE_BYTES, True), (MAX_SECRET_FILE_BYTES + 1, False)]
)
def test_the_renewed_document_is_bounded(tmp_path: Path, size: int, fits: bool) -> None:
    home = a_home_holding(tmp_path, b"x" * size)

    read = a_renewal().renewed_document(home)

    assert (read == b"x" * size) if fits else (read is SecretFileFault.TOO_LARGE)


def test_malformed_bytes_come_back_as_bytes_for_the_caller_to_judge(tmp_path: Path) -> None:
    home = a_home_holding(tmp_path, b"not json")

    renewed = a_renewal().renewed_document(home)

    assert renewed == b"not json"
    assert not isinstance(renewable_codex_login(renewed), RenewableCodexLogin)


def test_a_copy_the_cli_left_as_it_was_is_unchanged(tmp_path: Path) -> None:
    renewal = a_renewal()
    home = a_home_holding(tmp_path, made_due_copy(renewal.login, NOW))

    assert renewal.renewed_document(home) is CopyUnchanged.UNCHANGED


def test_a_document_with_a_wider_mode_is_read(tmp_path: Path) -> None:
    home = a_home_holding(tmp_path, a_codex_login())
    (home / "auth.json").chmod(0o644)

    assert a_renewal().renewed_document(home) == a_codex_login()


def test_nothing_secret_is_on_argv_in_the_environment_or_in_any_shown_form(tmp_path: Path) -> None:
    document = a_codex_login(
        tokens={"access_token": a_codex_access_token({"iat": 1, "exp": 3}) + CANARY_ACCESS_TOKEN}
    )
    renewal = a_renewal(login=a_login(document))
    start = renewal.start(tmp_path / "home", tmp_path / "work")
    causes = [
        renewal.renewed_document(tmp_path),
        CopyUnchanged.UNCHANGED,
        *SecretFileFault,
    ]

    visible = " ".join(
        [
            *start.child.arguments,
            *start.child.environment.values(),
            repr(renewal), str(renewal), repr(renewal.login), str(renewal.login), repr(start),
            *map(repr, causes), *map(str, causes),
        ]
    )
    assert [canary for canary in (*CANARIES, CANARY_ACCESS_TOKEN) if canary in visible] == []


@pytest.mark.parametrize("disclose", [pickle.dumps, copy.copy, copy.deepcopy])
@pytest.mark.parametrize("subject", ["renewal", "login"])
def test_a_renewal_and_its_login_refuse_pickling_and_copying(disclose, subject: str) -> None:
    renewal = a_renewal()

    with pytest.raises(TypeError):
        disclose(renewal if subject == "renewal" else renewal.login)


def test_every_entry_works_after_the_installed_configuration_is_reset(tmp_path: Path) -> None:
    reset_config()
    renewal = a_renewal()
    root = PrivateWorkingRoot(a_private_root(tmp_path))

    with run_directories(root, renewal.run_files()) as directories:
        start = renewal.start(directories.home, directories.working_directory)
        files = {path.name: path for path in directories.home.iterdir()}
        modes = {name: stat.S_IMODE(path.stat().st_mode) for name, path in files.items()}
        assert (stat.S_IMODE(directories.home.stat().st_mode), modes) == (
            0o700, {"auth.json": 0o600, "system-prompt": 0o600, "config.toml": 0o600},
        )
        assert json.loads(files["auth.json"].read_bytes())["tokens"]["access_token"] == ""
        assert renewal.renewed_document(directories.home) is CopyUnchanged.UNCHANGED
    assert start.child.environment["CODEX_HOME"] == str(directories.home)


def test_an_ordinary_run_of_the_same_login_still_holds_the_blanked_copy() -> None:
    login = a_login()
    ordinary = CodexRun(
        BINARY, assess_codex_login(login.document), "system", (_message(),), model=None,
        settings=(),
    )

    document = json.loads(ordinary.run_files()["auth.json"])
    assert document["tokens"]["refresh_token"] == ""
    assert document["tokens"]["access_token"] == CANARY_ACCESS_TOKEN
    assert "cli_auth_credentials_store" not in tomllib.loads(
        ordinary.run_files()["config.toml"].decode()
    )


def _message() -> TurnMessage:
    return TurnMessage(role=TurnRole.USER, text="hello")
