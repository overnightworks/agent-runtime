"""``GrokRenewal`` describes one renewal run of the pinned Grok CLI and reads its result back.

Every row builds the description from arguments only; the rows that read configuration back
run after ``reset_config()``.
"""

from __future__ import annotations

import copy
import json
import os
import pickle
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from tests.support.codex_support import a_codex_access_token
from tests.support.grok_support import a_renewing_fake_grok
from tests.support.provider_test_support import a_private_root

from agent_runtime.config import PrivateWorkingRoot, reset_config
from agent_runtime.grok.renewal import GrokRenewal
from agent_runtime.grok.run import GROK_CLI_PROCESS_POOL
from agent_runtime.grok_login import RenewableGrokLogin, renewable_grok_login
from agent_runtime.login_renewal import CopyUnchanged
from agent_runtime.process import run_cli_bounded, run_directories
from agent_runtime.secret_file import SecretFileFault
from agent_runtime.spawn import ChildProcessSpecificationError, closed_environment

BINARY = Path("/opt/grok/bin/grok")
NOW = 1_800_000_000
KEY_LIFETIME_SECONDS = 6 * 3600
UNREADABLE_CEILING_SECONDS = 24 * 3600
REFRESH_CANARY = "canary-renewal-refresh-member"


def a_document(key: str, *, refresh_token: str = REFRESH_CANARY, **realm: object) -> bytes:
    return json.dumps({"xai": {"key": key, "refresh_token": refresh_token, **realm}}).encode()


def a_key(*, issued_at: int = NOW, lifetime: int = KEY_LIFETIME_SECONDS) -> str:
    return a_codex_access_token({"iat": issued_at, "exp": issued_at + lifetime})


def a_login(document: bytes | None = None) -> RenewableGrokLogin:
    login = renewable_grok_login(document or a_document(a_key()))
    assert isinstance(login, RenewableGrokLogin)
    return login


def a_renewal(**changes: Any) -> GrokRenewal:
    fields: dict[str, Any] = {"binary": BINARY, "login": a_login(), "now": NOW}
    return GrokRenewal(**{**fields, **changes})


def a_home_holding(directory: Path, document: bytes) -> Path:
    home = directory / "home"
    home.mkdir()
    (home / "auth.json").write_bytes(document)
    return home


def threshold_of(renewal: GrokRenewal, directory: Path) -> int:
    start = renewal.start(directory / "home", directory / "work")
    return int(start.child.environment["GROK_AUTH_EARLY_INVALIDATION_SECS"])


def test_the_run_files_are_exactly_the_unaltered_full_login() -> None:
    renewal = a_renewal()

    assert renewal.run_files() == {"auth.json": renewal.login.document}
    assert REFRESH_CANARY.encode() in renewal.run_files()["auth.json"]


def test_the_run_is_grok_models_with_the_home_variables_and_the_threshold(tmp_path: Path) -> None:
    home, work = tmp_path / "home", tmp_path / "work"
    renewal = a_renewal(now=NOW + 2 * 3600)

    start = renewal.start(home, work)

    remaining = KEY_LIFETIME_SECONDS - 2 * 3600
    assert start.child.arguments == ("models",)
    assert start.child.environment == {
        **closed_environment(home),
        "GROK_HOME": str(home),
        "GROK_AUTH_EARLY_INVALIDATION_SECS": str(remaining + 3600),
    }
    assert (start.child.binary, start.child.working_directory, start.stdin) == (BINARY, work, b"")
    assert renewal.pool is GROK_CLI_PROCESS_POOL
    assert int(start.child.environment["GROK_AUTH_EARLY_INVALIDATION_SECS"]) > remaining


def test_the_threshold_at_the_half_life_of_the_key_is_below_its_whole_lifetime(
    tmp_path: Path,
) -> None:
    renewal = a_renewal(now=NOW + KEY_LIFETIME_SECONDS // 2)

    assert threshold_of(renewal, tmp_path) < KEY_LIFETIME_SECONDS


def test_an_expired_key_still_gets_a_threshold_past_now(tmp_path: Path) -> None:
    assert threshold_of(a_renewal(now=NOW + 2 * KEY_LIFETIME_SECONDS), tmp_path) > 0


@pytest.mark.parametrize(
    "key",
    ["opaque-key", a_codex_access_token({"iat": 1})],
    ids=["not-a-jwt", "claim-missing"],
)
def test_an_unreadable_key_gets_the_fixed_ceiling_and_the_run_still_starts(
    tmp_path: Path, key: str
) -> None:
    renewal = a_renewal(login=a_login(a_document(key)))

    assert threshold_of(renewal, tmp_path) == UNREADABLE_CEILING_SECONDS


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"binary": Path("grok")}, id="relative-binary"),
        pytest.param({"login": RenewableGrokLogin(b"not json")}, id="login-not-usable"),
        pytest.param(
            {"login": RenewableGrokLogin(json.dumps({"x": {"key": "k"}}).encode())},
            id="login-without-refresh-member",
        ),
        pytest.param({"login": object()}, id="foreign-login"),
    ],
)
def test_a_renewal_that_could_not_run_is_refused_at_construction(changes: dict[str, Any]) -> None:
    with pytest.raises(ChildProcessSpecificationError):
        a_renewal(**changes)


def test_the_renewed_document_is_what_the_cli_left_in_the_home(tmp_path: Path) -> None:
    renewed = a_document(a_key(), refresh_token="rotated")

    assert a_renewal().renewed_document(a_home_holding(tmp_path, renewed)) == renewed


def test_a_login_the_cli_left_as_it_was_is_unchanged(tmp_path: Path) -> None:
    renewal = a_renewal()
    home = a_home_holding(tmp_path, renewal.login.document)

    assert renewal.renewed_document(home) is CopyUnchanged.UNCHANGED


def test_a_missing_login_is_a_fault(tmp_path: Path) -> None:
    assert a_renewal().renewed_document(tmp_path) is SecretFileFault.MISSING


def test_a_linked_login_is_refused_and_its_target_is_not_read(tmp_path: Path) -> None:
    (tmp_path / "target").write_bytes(a_document(a_key()))
    (tmp_path / "auth.json").symlink_to(tmp_path / "target")

    assert a_renewal().renewed_document(tmp_path) is SecretFileFault.LINK


def test_an_oversized_login_is_a_fault(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_bytes(b"x" * (1024 * 1024))

    assert isinstance(a_renewal().renewed_document(tmp_path), SecretFileFault)


def test_a_fifo_login_is_a_fault_and_does_not_block(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "auth.json")

    assert isinstance(a_renewal().renewed_document(tmp_path), SecretFileFault)


def test_nothing_secret_is_in_any_shown_form_of_the_renewal_or_its_start(tmp_path: Path) -> None:
    renewal = a_renewal()
    start = renewal.start(tmp_path / "home", tmp_path / "work")

    visible = " ".join(
        [
            *start.child.arguments,
            repr(renewal),
            str(renewal),
            repr(renewal.login),
            repr(start),
        ]
    )
    assert REFRESH_CANARY not in visible


@pytest.mark.parametrize("disclose", [pickle.dumps, copy.copy, copy.deepcopy])
@pytest.mark.parametrize("subject", ["renewal", "login"])
def test_a_renewal_and_its_login_refuse_pickling_and_copying(disclose, subject: str) -> None:
    renewal = a_renewal()

    with pytest.raises(TypeError):
        disclose(renewal if subject == "renewal" else renewal.login)


def test_a_fake_grok_renews_the_login_through_the_real_directories_and_runner(
    tmp_path: Path,
) -> None:
    reset_config()
    renewed = a_document(a_key(issued_at=NOW + 5), refresh_token="rotated")
    binary = a_renewing_fake_grok(tmp_path, renewed, threshold_at_least=KEY_LIFETIME_SECONDS + 1)
    renewal = a_renewal(binary=binary, now=NOW)
    root = PrivateWorkingRoot(a_private_root(tmp_path))

    with run_directories(root, renewal.run_files()) as directories:
        start = renewal.start(directories.home, directories.working_directory)
        modes = {
            path.name: stat.S_IMODE(path.stat().st_mode) for path in directories.home.iterdir()
        }
        outcome = run_cli_bounded(
            start.child, stdin_payload=start.stdin, read="all", deadline=_deadline()
        )
        read_back = renewal.renewed_document(directories.home)

    assert (modes, outcome.returncode) == ({"auth.json": 0o600}, 0)
    assert read_back == renewed


def _deadline() -> float:
    return time.monotonic() + 30
