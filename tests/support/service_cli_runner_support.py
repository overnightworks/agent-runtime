"""The tool-free Claude runner over HTTP, shared by the service's runner and Codex test modules.

The app is served on a loopback socket and a fake Claude program runs as a real child.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import signal
import socket
import ssl
import struct
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import uvicorn
from fastapi import FastAPI
from tests.support.anchored_support import GONE, ChildCleanup
from tests.support.bounds import TEST_WAIT_SECONDS
from tests.support.mcp_app_fake import TestAuthority
from tests.support.provider_test_support import (
    SYSTEM_TEXT,
    ScriptedClaudeChild,
    a_fake_claude,
    a_private_root,
    held_claude_slots,
    own_real_children,
    the_only_tree,
)
from tests.support.service_support import (
    ALICE,
    PINNED_CLAUDE_VERSION,
    StepClock,
    TokenMint,
    alice_admitted_run,
    decoded_events,
    failure_lines,
    get_run,
    instant,
    post_run,
    run_body,
    run_headers,
    served,
    service_config,
)

import agent_runtime_service.app as app_module
import agent_runtime_service.config as config_module
from agent_runtime import lost_groups, spawn
from agent_runtime.config import PrivateWorkingRoot
from agent_runtime_client.wire import (
    RunEvent,
    RunStatus,
)
from agent_runtime_service import cli_runner
from agent_runtime_service.admission import AdmissionSlot
from agent_runtime_service.app import create_app
from agent_runtime_service.capability import Capability
from agent_runtime_service.cli import installed_runners, main
from agent_runtime_service.config import McpConfig, ServiceConfig
from agent_runtime_service.quarantine import Quarantine
from agent_runtime_service.run_events import (
    run_frames,
)
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.run_records import RunRecords
from agent_runtime_service.runners import Runners

MINT = TokenMint()


CANARY = "cli-text-" + uuid.uuid4().hex


PLAIN_APP = {"mcp_origin": None}


VERSION_LINE = f"{PINNED_CLAUDE_VERSION} (Claude Code)"


THREE_MESSAGE_TURN = {
    "system": SYSTEM_TEXT,
    "messages": [
        {"role": "user", "text": "a"},
        {"role": "assistant", "text": "b"},
        {"role": "user", "text": "c"},
    ],
}


def a_config(
    tmp_path: Path,
    binary: Path,
    *,
    runs: dict[str, object] | None = None,
    songmaker: dict[str, object] | None = None,
    token_file: Path | None = None,
) -> ServiceConfig:
    config = service_config(
        runs=runs,
        songmaker=PLAIN_APP if songmaker is None else songmaker,
        claude_binary=binary,
        token_file=token_file,
    )
    private_root = PrivateWorkingRoot(a_private_root(tmp_path))
    return config.model_copy(
        update={"cli": config.cli.model_copy(update={"working_root": private_root})}
    )


def trusting(config: ServiceConfig, authority: TestAuthority) -> ServiceConfig:
    return config.model_copy(update={"mcp": McpConfig(ca_file=str(authority.ca_file))})


def tool_free_runners(config: ServiceConfig) -> Runners:
    return installed_runners(config, None, LoopClock())


def private_root_of(config: ServiceConfig) -> Path:
    return config.cli.working_root.path


def drive_over_http[T](
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    config: ServiceConfig,
    scenario: Callable[[str], T],
    runners: Runners | None = None,
) -> T:
    """Serve the app with ``runners`` (the installed ones by default) and run ``scenario``.

    The fake program's children are owned by ``child_cleanup`` and their watchers joined.
    """
    app = create_app(
        config,
        runners=runners or tool_free_runners(config),
        fetch_key_set=MINT.fetch_key_set,
    )

    def serve() -> T:
        with served(app) as base_url:
            return scenario(base_url)

    return own_real_children(child_cleanup, monkeypatch, serve)


def headers() -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user())


def post_a_run(base_url: str, tools: Mapping[str, str] | None = None) -> list[RunEvent]:
    body = run_body(turn=THREE_MESSAGE_TURN, tools=dict(tools or {}))
    return decoded_events(post_run(base_url, headers(), body).content)


def shown(events: list[RunEvent]) -> list[tuple[str, str | None]]:
    return [
        (event.type, getattr(event, "text", None) or getattr(event, "message", None))
        for event in events[1:]
    ]


def status_of(base_url: str, events: list[RunEvent]) -> RunStatus:
    response = get_run(base_url, headers(), str(events[0].run_id))
    return RunStatus.model_validate_json(response.content)


def all_children_gone(child_cleanup: ChildCleanup) -> list[bool]:
    return [child_cleanup.state(owned) is GONE for owned in child_cleanup.listed()]


LOGIN_FILE_NAME = "claude-login.token"


FIRST_LOGIN = "first-login-" + uuid.uuid4().hex


SECOND_LOGIN = "second-login-" + uuid.uuid4().hex


def swap_login(login_file: Path, token: str) -> None:
    """Write the login beside the file and move it over the file, as ``mv -T`` does."""
    beside = login_file.with_name(login_file.name + ".new")
    beside.write_text(token + "\n", encoding="utf-8")
    beside.chmod(0o600)
    beside.replace(login_file)


def a_login_file(tmp_path: Path, token: str) -> Path:
    login_file = tmp_path / LOGIN_FILE_NAME
    swap_login(login_file, token)
    return login_file


def login_the_child_saw(capture: Path) -> str:
    environment = (capture / "environment").read_text()
    found = re.search(r"^CLAUDE_CODE_OAUTH_TOKEN=(.*)$", environment, re.MULTILINE)
    assert found is not None
    return found[1]


def gone(login_file: Path) -> None:
    login_file.unlink()


def open_to_everyone(login_file: Path) -> None:
    login_file.chmod(0o644)


def holding_json(login_file: Path) -> None:
    swap_login(login_file, json.dumps({"token": CANARY}))


LOGIN_FILES_GONE_BAD = [
    pytest.param(gone, id="missing"),
    pytest.param(open_to_everyone, id="mode-0644"),
    pytest.param(holding_json, id="json"),
]


def assert_only_the_class_was_logged(
    caplog: pytest.LogCaptureFixture, failed: list[RunEvent], tmp_path: Path
) -> None:
    assert [(event, kind) for event, kind, _ in failure_lines(caplog)] == [
        (f"The runner of run {failed[0].run_id} failed", "UnsoundSecretFile")
    ]
    leaks = (LOGIN_FILE_NAME, str(tmp_path), CANARY, FIRST_LOGIN, SECOND_LOGIN)
    assert [leak for leak in leaks if leak in caplog.text] == []


class HeldReap:
    """A gate in front of the real group reap: it waits, off the loop, until the test lets go."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.let_go = threading.Event()
        self.reaps = 0

    def wait_until_reached(self) -> None:
        assert self.reached.wait(TEST_WAIT_SECONDS), "the run was never stopped"


@pytest.fixture(name="held_reap")
def a_held_reap(monkeypatch: pytest.MonkeyPatch) -> Iterator[HeldReap]:
    hold = HeldReap()
    reap = spawn.AnchoredChild.kill_until_gone

    async def held(child: spawn.AnchoredChild, *, within: float) -> spawn.KillUntilGoneResult:
        hold.reaps += 1
        hold.reached.set()
        await asyncio.to_thread(hold.let_go.wait, TEST_WAIT_SECONDS)
        return await reap(child, within=within)

    monkeypatch.setattr(spawn.AnchoredChild, "kill_until_gone", held)
    yield hold
    hold.let_go.set()


POLL_INTERVAL_SECONDS = 0.02


def eventually(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + TEST_WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(POLL_INTERVAL_SECONDS)


def leave_after(base_url: str, event_type: str) -> uuid.UUID:
    """Start a run, read its stream up to ``event_type`` and reset the connection.

    A reset reaches the server as a lost connection without a write from it.
    """
    body = json.dumps(run_body(turn=THREE_MESSAGE_TURN)).encode()
    request_headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Connection": "close",
        **headers(),
    }
    head = "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
    host, port = urlsplit(base_url).netloc.split(":")
    with socket.create_connection((host, int(port)), timeout=TEST_WAIT_SECONDS) as client:
        client.sendall(f"POST /runs HTTP/1.1\r\nHost: {host}\r\n{head}\r\n".encode() + body)
        received = b""
        while f'"type":"{event_type}"'.encode() not in received:
            chunk = client.recv(4096)
            assert chunk, f"the stream ended before {event_type}"
            received += chunk
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    match = re.search(rb'"run_id":"([0-9a-f-]{36})"', received)
    assert match is not None
    return uuid.UUID(match.group(1).decode())


@dataclass
class ScriptedRun:
    """A run of the real runner over a scripted child, read frame by frame from the stream."""

    frames: AsyncGenerator[bytes, None]
    slot: AdmissionSlot
    records: RunRecords
    clock: StepClock
    run_id: uuid.UUID
    root: Path

    def place_is_held(self) -> bool:
        probe = self.slot.try_take()
        if probe is not None:
            probe.release()
        return probe is None

    def status(self) -> RunStatus:
        status = self.records.read(self.run_id, "songmaker", ALICE)
        assert status is not None
        return status

    def held_state(self) -> tuple[bool, int, bool]:
        return self.place_is_held(), held_claude_slots(), the_only_tree(self.root).is_dir()

    async def frees_its_place_once_the_group_is_confirmed(self, child: ScriptedClaudeChild) -> None:
        retry = next(
            task for task in asyncio.all_tasks() if task.get_name().startswith("quarantine:")
        )
        self.clock.advance_to(await self.clock.parked() or 0)
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            while (deadline := await self.clock.parked()) is None:
                await asyncio.sleep(0.01)
        assert self.held_state() == (True, 1, True)
        child.publish_exit(-signal.SIGKILL)
        self.clock.advance_to(deadline)
        await asyncio.wait_for(retry, TEST_WAIT_SECONDS)
        assert (self.place_is_held(), held_claude_slots(), list(self.root.iterdir())) == (
            False,
            0,
            [],
        )


def a_scripted_run(
    tmp_path: Path,
    *,
    config: ServiceConfig | None = None,
    tls_context: ssl.SSLContext | None = None,
    capability: Capability | None = None,
) -> ScriptedRun:
    config = config or a_config(tmp_path, a_fake_claude(tmp_path))
    clock = StepClock()
    slot = AdmissionSlot(1)
    records = RunRecords(lambda: instant(clock.now))
    run_id = uuid.uuid4()
    frames = run_frames(
        dataclasses.replace(alice_admitted_run(run_id), capability=capability),
        cli_runner.CliRunner(config, tls_context=tls_context, clock=clock),
        config.runs,
        clock,
        records,
        slot.try_take(),
        quarantine=Quarantine(clock, lost_groups.confirm_group_gone),
    )
    return ScriptedRun(frames, slot, records, clock, run_id, private_root_of(config))


def through_the_service_entry[Result](
    tmp_path: Path,
    config: ServiceConfig,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Callable[[str], Result],
) -> Result:
    """Start ``cli.main`` on ``config``, serve the app it builds and play ``scenario`` on it."""
    served_apps: list[FastAPI] = []
    create_app_for_the_test = app_module.create_app

    def create_app_with_the_test_key_set(built: ServiceConfig, **keywords: Any):
        return create_app_for_the_test(built, fetch_key_set=MINT.fetch_key_set, **keywords)

    monkeypatch.setattr(uvicorn, "run", lambda built, **_where: served_apps.append(built))
    monkeypatch.setattr(config_module, "load_config", lambda _path: config)
    monkeypatch.setattr(app_module, "create_app", create_app_with_the_test_key_set)

    def start_and_play() -> Result:
        assert main(["--config", str(tmp_path / "agent-runtime-service.toml")]) == 0
        with served(served_apps[0]) as base_url:
            return scenario(base_url)

    return own_real_children(child_cleanup, monkeypatch, start_and_play)
