"""The service renews its dedicated Codex login from its own lifespan.

Rows build ``renewals_of`` from real configurations and drive the app's lifespan; the last three
start a real ``agent-runtime-service`` process and stop it with SIGTERM.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.support.anchored_support import ChildCleanup
from tests.support.bounds import TEST_WAIT_SECONDS, within_the_bound
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.provider_test_support import own_real_children
from tests.support.service_cli_runner_support import (
    MINT,
    THREE_MESSAGE_TURN,
    eventually,
    headers,
    shown,
)
from tests.support.service_renewal_support import (  # noqa: F401 - the autouse fixture
    BlockedHook,
    CodexServiceFiles,
    a_due_login_now,
    a_fake_runner,
    a_login_issued_at,
    a_renewed_login_now,
    codex_service_files,
    the_held_ends_confirmed,
    writing,
)
from tests.support.service_support import (
    decoded_events,
    run_body,
    service_config,
)
from tests.support.service_table_support import (
    CODEX_TABLE,
    a_config_with_both_providers,
    a_table_service,
    write_login,
)

from agent_runtime_service.app import create_app
from agent_runtime_service.cli import installed_runners
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.login_file import swap_sibling
from agent_runtime_service.renewal_schedule import renewals_of
from agent_runtime_service.run_liveness import LoopClock

A_MOMENT_SECONDS = 0.5
POLL_SECONDS = 0.05


def a_renewing_app(config: ServiceConfig):
    return create_app(
        config,
        runners=installed_runners(config, None, LoopClock()),
        fetch_key_set=MINT.fetch_key_set,
        renewals=renewals_of(config.cli),
    )


def a_due_codex_service(tmp_path: Path, **fake: Any) -> tuple[ServiceConfig, Path, Path]:
    """The Codex table's config, its stand-in capture directory and its login, due for renewal."""
    config, capture = a_table_service(CODEX_TABLE, tmp_path, **fake)
    login = write_login(tmp_path / CODEX_TABLE.login_name, a_due_login_now())
    return config, capture, login


def post_run_through(client: TestClient) -> list[Any]:
    response = client.post(
        "/runs",
        headers={"Content-Type": "application/json", **headers()},
        content=json.dumps(run_body(turn=THREE_MESSAGE_TURN)),
    )
    return decoded_events(response.content)


def test_serving_starts_at_once_while_the_first_renewal_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It renews after ``reset_config()``, which the autouse fixture runs before every test."""
    config, _, login = a_due_codex_service(tmp_path)
    renewed = a_renewed_login_now()
    hook = BlockedHook(writing(renewed))
    a_fake_runner(monkeypatch, on_home=hook)

    with TestClient(a_renewing_app(config)) as client:
        assert hook.entered.wait(TEST_WAIT_SECONDS)
        unknown = client.get(f"/runs/{uuid.uuid4()}", headers=headers())
        hook.release()

    assert unknown.status_code == HTTPStatus.NOT_FOUND
    assert login.read_bytes() == renewed


def test_a_codex_run_is_served_during_a_renewal_and_reads_the_whole_old_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_cleanup: ChildCleanup,
) -> None:
    config, capture, login = a_due_codex_service(tmp_path)
    old_document = login.read_bytes()
    renewed = a_renewed_login_now()
    hook = BlockedHook(writing(renewed))
    a_fake_runner(monkeypatch, on_home=hook)
    sibling = swap_sibling(login)

    def scenario() -> tuple[list[Any], bool, str, list[Any], str]:
        with TestClient(a_renewing_app(config)) as client:
            assert hook.entered.wait(TEST_WAIT_SECONDS)
            during = post_run_through(client)
            sibling_held = sibling.exists()
            login_of_that_run = (capture / "auth.json").read_text()
            hook.release()
            eventually(lambda: login.read_bytes() == renewed)
            after = post_run_through(client)
            login_of_the_next_run = (capture / "auth.json").read_text()
            return during, sibling_held, login_of_that_run, after, login_of_the_next_run

    during, sibling_held, login_during, after, login_after = own_real_children(
        child_cleanup, monkeypatch, scenario
    )

    assert shown(during)[-1][0] == "succeeded"
    assert sibling_held is True
    assert _access_token_of(old_document) in login_during
    assert shown(after)[-1][0] == "succeeded"
    assert _access_token_of(renewed) in login_after


def _access_token_of(document: bytes) -> str:
    return json.loads(document)["tokens"]["access_token"]


def test_a_configuration_without_a_codex_table_starts_no_task_and_no_thread() -> None:
    schedule = renewals_of(service_config().cli)
    threads_before = set(threading.enumerate())

    async def scenario() -> tuple[int, set[threading.Thread]]:
        tasks_before = len(asyncio.all_tasks())
        async with schedule.running():
            return len(asyncio.all_tasks()) - tasks_before, set(threading.enumerate())

    started_tasks, threads_during = asyncio.run(scenario())

    assert started_tasks == 0
    assert threads_during == threads_before


def test_the_codex_table_alone_is_renewed_with_its_binary_in_a_home_under_the_working_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_config_with_both_providers(tmp_path)
    write_login(tmp_path / CODEX_TABLE.login_name, a_due_login_now())
    runner = a_fake_runner(monkeypatch, on_home=writing(a_renewed_login_now()))
    schedule = renewals_of(config.cli)

    async def scenario() -> None:
        async with schedule.running():
            while runner.calls == 0:
                await asyncio.sleep(0)

    asyncio.run(within_the_bound(scenario()))

    (child,) = runner.children
    assert child.binary == config.cli.openai.binary
    assert Path(child.environment["CODEX_HOME"]).is_relative_to(config.cli.working_root.path)


@contextmanager
def a_serving_process(files: CodexServiceFiles, port: int) -> Iterator[subprocess.Popen[bytes]]:
    """A real ``agent-runtime-service`` for ``files``, killed when the scope ends."""
    with (files.configuration.parent / "service.log").open("wb") as output:
        process = subprocess.Popen(  # noqa: S603 - the test's own interpreter and file
            [
                sys.executable,
                "-m",
                "agent_runtime_service.cli",
                "--config",
                str(files.configuration),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=files.configuration.parent,
        )
        try:
            _wait_until_serving(process, port)
            yield process
        finally:
            process.kill()
            process.wait(TEST_WAIT_SECONDS)


def _a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_serving(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + TEST_WAIT_SECONDS
    while True:
        assert process.poll() is None, "the service ended before it served"
        assert time.monotonic() < deadline, "the service never served"
        try:
            socket.create_connection(("127.0.0.1", port), timeout=POLL_SECONDS).close()
        except OSError:
            time.sleep(POLL_SECONDS)
            continue
        return


def test_sigterm_during_a_renewal_ends_the_process_only_after_the_renewal_committed(
    tmp_path: Path,
) -> None:
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    give_up_after = int(TEST_WAIT_SECONDS / POLL_SECONDS)
    blocked = (
        f": > {shlex.quote(str(entered))}\n"
        f"waited=0\n"
        f"while [ ! -e {shlex.quote(str(release))} ] && [ $waited -lt {give_up_after} ]; do\n"
        f"  sleep {POLL_SECONDS}; waited=$((waited + 1))\n"
        "done"
    )
    renewed = a_renewed_login_now()
    port = _a_free_port()
    files = codex_service_files(tmp_path, a_due_login_now(), renewed, then=blocked, port=port)
    with a_serving_process(files, port) as process:
        try:
            eventually(entered.exists)
            process.send_signal(signal.SIGTERM)
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(A_MOMENT_SECONDS)
        finally:
            release.touch()
        returncode = process.wait(TEST_WAIT_SECONDS)

    assert returncode == -signal.SIGTERM
    assert files.login.read_bytes() == renewed
    assert not swap_sibling(files.login).exists()
    assert list(files.root.iterdir()) == []


def test_sigterm_while_the_schedule_pauses_after_a_renewal_ends_the_process_promptly(
    tmp_path: Path,
) -> None:
    renewed = a_renewed_login_now()
    port = _a_free_port()
    files = codex_service_files(tmp_path, a_due_login_now(), renewed, port=port)
    with a_serving_process(files, port) as process:
        eventually(lambda: files.login.read_bytes() == renewed)
        process.send_signal(signal.SIGTERM)
        returncode = process.wait(TEST_WAIT_SECONDS)

    assert returncode == -signal.SIGTERM
    assert files.login.read_bytes() == renewed
    assert not swap_sibling(files.login).exists()
    assert list(files.root.iterdir()) == []


def test_sigterm_while_nothing_is_due_ends_the_process_with_its_login_untouched(
    tmp_path: Path,
) -> None:
    port = _a_free_port()
    files = codex_service_files(
        tmp_path, a_login_issued_at(int(time.time())), a_renewed_login_now(), port=port
    )
    with a_serving_process(files, port) as process:
        process.send_signal(signal.SIGTERM)
        returncode = process.wait(TEST_WAIT_SECONDS)

    assert returncode == -signal.SIGTERM
    assert files.traces() == files.traces_without_a_renewal()
