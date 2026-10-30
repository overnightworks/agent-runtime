"""Fake CLIs that talk to the run's relay, and a TLS fake app, for the CLI runner's tool path.

``a_relay_calling_claude`` writes an executable Python stand-in: it reads ``$HOME/relay-mcp.json``,
speaks ``initialize``, ``tools/list`` and ``tools/call`` to the relay with the bearer header
the file names, announces the relay as connected, optionally prints the CLI's own
``tool_use``/``tool_result`` lines, records every start fact and every reply it received under
``capture``, and ends with a ``result`` line. A refused
``tools/list`` leaves it with no tools, and ``give_up_when_present`` makes it give each call up
(``notifications/cancelled``, then a closed connection) once that file exists, instead of waiting.

``a_relay_calling_codex`` is Codex's stand-in: it reads the relay entry from
``$CODEX_HOME/config.toml`` and the secret from the variable that entry names, records what it
was given the same way, and prints ``exec --json`` lines around its calls.
"""

from __future__ import annotations

import json
import socket
import stat
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tests.support.descendant_support import Handshake
from tests.support.mcp_app_fake import (
    CERTIFICATE_HOST,
    McpAppFake,
    RecordedRequest,
    Responder,
    ScriptedApp,
    TestAuthority,
    serving,
    tool_listing,
)

STRAY_TOOL_LINES = (
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "toolu_stray", "name": "mcp__relay__stray", "input": {}}
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "toolu_stray", "content": "stray"}]
        },
    },
)

_CLAUDE_SOURCE = """\
#!{python}
import http.client, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

PLAN = json.loads({plan!r})
if sys.argv[1:] == ["--version"]:
    print(PLAN["version_line"])
    sys.exit(0)
stdin_line = sys.stdin.readline()
home = os.environ["HOME"]
capture = PLAN["capture"]
marker = None
if PLAN["marked_by_system_prompt"]:
    marker = open(home + "/system-prompt").read()
    capture = capture + "/" + marker
os.makedirs(capture + "/files", exist_ok=True)
with open(capture + "/stdin", "w") as handle:
    handle.write(stdin_line)
with open(capture + "/argv", "w") as handle:
    handle.write("\\n".join(sys.argv[1:]))
with open(capture + "/environment", "w") as handle:
    handle.write("\\n".join(f"{{k}}={{v}}" for k, v in os.environ.items()))
for name in os.listdir(home):
    with open(home + "/" + name, "rb") as source, open(capture + "/files/" + name, "wb") as copy:
        copy.write(source.read())
relay = json.load(open(home + "/relay-mcp.json"))["mcpServers"]["relay"]
first_run = True
if PLAN["descendant"]:
    program, identity, ack, ready = PLAN["descendant"]
    try:
        os.close(os.open(program + ".started", os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        first_run = False
    else:
        subprocess.Popen(
            [sys.executable, "-I", program, identity, ack, ready, "0"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(ready, "rb") as published:
            published.read(1)


def rpc(method, params=None, request_id=None, version=None):
    body = {{"jsonrpc": "2.0", "method": method}}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    headers = {{**relay["headers"], "Content-Type": "application/json"}}
    if version:
        headers["MCP-Protocol-Version"] = version
    request = urllib.request.Request(
        relay["url"], data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=PLAN["timeout"]) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, ConnectionError, OSError):
        return None
    with open(capture + "/replies", "a") as handle:
        reply = {{"method": method, "status": status, "body": raw.decode()}}
        handle.write(json.dumps(reply) + "\\n")
    return json.loads(raw) if raw else None


def wait_for(path):
    deadline = time.monotonic() + PLAN["timeout"]
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.02)


def call_then_give_up(arguments, request_id, version):
    address = urllib.parse.urlsplit(relay["url"])
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=PLAN["timeout"])
    body = {{"jsonrpc": "2.0", "method": "tools/call", "id": request_id, "params": arguments}}
    headers = {{
        **relay["headers"], "Content-Type": "application/json", "MCP-Protocol-Version": version
    }}
    connection.request("POST", address.path, body=json.dumps(body), headers=headers)
    wait_for(PLAN["give_up_when_present"])
    rpc("notifications/cancelled", {{"requestId": request_id}}, version=version)
    connection.close()


def emit(payload):
    print(json.dumps(payload), flush=True)


listing = None
version = None
if PLAN["relay_status"] == "connected":
    version = rpc("initialize", {{"protocolVersion": "2025-11-25"}}, 1)["result"]["protocolVersion"]
    rpc("notifications/initialized", version=version)
    listing = (rpc("tools/list", {{}}, 2, version) or {{}}).get("result", {{}}).get("tools")
tool_names = PLAN["builtin_tools"] + ["mcp__relay__" + tool["name"] for tool in listing or []]
emit({{
    "type": "system", "subtype": "init", "tools": tool_names, "apiKeySource": "none",
    "mcp_servers": [{{"name": "relay", "status": PLAN["relay_status"]}}],
}})
time.sleep(PLAN["linger_before_calls_seconds"])
for number, call in enumerate(PLAN["calls"], start=1):
    for stray in PLAN["stray_lines"]:
        emit(stray)
    marked = {{"marker": marker}} if marker else {{}}
    arguments = {{"name": call["name"], "arguments": {{**call["arguments"], **marked}}}}
    if PLAN["give_up_when_present"]:
        call_then_give_up(arguments, 10 + number, version)
    else:
        rpc("tools/call", arguments, 10 + number, version)
for stray in PLAN["stray_lines"]:
    emit(stray)
time.sleep(PLAN["linger_seconds"] if first_run else 0)
if PLAN["exit_code"]:
    sys.exit(PLAN["exit_code"])
emit({{"type": "result", "subtype": "success", "is_error": False, "result": "done"}})
"""


def _paths_of(handshake: Handshake) -> list[str]:
    paths = (handshake.program, handshake.identity, handshake.ack, handshake.ready)
    return [str(path) for path in paths]


@dataclass(frozen=True)
class ClaudeCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


def a_relay_calling_claude(
    directory: Path,
    *,
    version: str,
    calls: Sequence[ClaudeCall] = (),
    relay_status: str = "connected",
    stray_tool_lines: bool = False,
    exit_code: int = 0,
    linger_seconds: float = 0.0,
    linger_before_calls_seconds: float = 0.0,
    give_up_when_present: Path | None = None,
    timeout_seconds: float = 20.0,
    marked_by_system_prompt: bool = False,
    descendant: Handshake | None = None,
    builtin_tools: Sequence[str] = (),
) -> Path:
    """The pinned program's stand-in for a relay run; it captures into ``directory/capture``.

    With ``marked_by_system_prompt`` one executable serves parallel runs: the run's system text
    is its marker, naming its capture directory ``capture/<marker>`` and travelling in every
    call's arguments as ``marker``.

    ``builtin_tools`` are the names its init announces beside the relay's tools.

    With ``descendant`` the first run's program starts a SIGTERM-ignoring child of its own
    process group that stays in the group (``descendant_support``'s program), and waits for its
    ready signal; the test's ``DescendantRow`` completes the handshake. Only that first run
    lingers for ``linger_seconds``; a later run ends at once.
    """
    plan = {
        "version_line": f"{version} (Claude Code)",
        "capture": str(directory / "capture"),
        "calls": [{"name": call.name, "arguments": dict(call.arguments)} for call in calls],
        "relay_status": relay_status,
        "stray_lines": list(STRAY_TOOL_LINES) if stray_tool_lines else [],
        "exit_code": exit_code,
        "linger_seconds": linger_seconds,
        "linger_before_calls_seconds": linger_before_calls_seconds,
        "give_up_when_present": None if give_up_when_present is None else str(give_up_when_present),
        "timeout": timeout_seconds,
        "marked_by_system_prompt": marked_by_system_prompt,
        "descendant": None if descendant is None else _paths_of(descendant),
        "builtin_tools": list(builtin_tools),
    }
    binary = directory / "claude"
    binary.write_text(_CLAUDE_SOURCE.format(python=sys.executable, plan=json.dumps(plan)))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


_CODEX_SOURCE = """\
#!{python}
import json, os, sys, tomllib, urllib.error, urllib.request

PLAN = json.loads({plan!r})
if sys.argv[1:] == ["--version"]:
    print(PLAN["version_line"])
    sys.exit(0)
stdin = sys.stdin.read()
home = os.environ["CODEX_HOME"]
capture = PLAN["capture"]
os.makedirs(capture + "/files", exist_ok=True)
with open(capture + "/stdin", "w") as handle:
    handle.write(stdin)
with open(capture + "/argv", "w") as handle:
    handle.write("\\n".join(sys.argv[1:]))
with open(capture + "/environment", "w") as handle:
    handle.write("\\n".join(f"{{k}}={{v}}" for k, v in os.environ.items()))
for name in os.listdir(home):
    with open(home + "/" + name, "rb") as source, open(capture + "/files/" + name, "wb") as copy:
        copy.write(source.read())
relay = tomllib.load(open(home + "/config.toml", "rb"))["mcp_servers"].get("relay")
bearer = PLAN["wrong_bearer"] or (relay and os.environ[relay["bearer_token_env_var"]])


def rpc(method, params=None, request_id=None, version=None):
    body = {{"jsonrpc": "2.0", "method": method}}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    headers = {{"Authorization": "Bearer " + bearer, "Content-Type": "application/json"}}
    if version:
        headers["MCP-Protocol-Version"] = version
    request = urllib.request.Request(
        relay["url"], data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=PLAN["timeout"]) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    reply = {{"method": method, "status": status, "body": raw.decode()}}
    with open(capture + "/replies", "a") as handle:
        handle.write(json.dumps(reply) + "\\n")
    return status, (json.loads(raw) if raw else None)


def connect():
    status, answer = rpc("initialize", {{"protocolVersion": "2025-11-25"}}, 1)
    if status != 200:
        # codex 0.163.0 ends so when its required server refuses (ADR A1b-X0c-0).
        sys.exit(1)
    version = answer["result"]["protocolVersion"]
    rpc("notifications/initialized", version=version)
    rpc("tools/list", {{}}, 2, version)
    return version


def emit(payload):
    print(json.dumps(payload), flush=True)


def item(event_type, item_type, **fields):
    emit({{"type": event_type, "item": {{"id": "item_" + item_type, "type": item_type, **fields}}}})


version = connect() if PLAN["lists"] == "first" else None
emit({{"type": "thread.started", "thread_id": "thread"}})
emit({{"type": "turn.started"}})
if PLAN["lists"] == "after-text":
    item("item.completed", "agent_message", text="first")
    version = connect()
for number, call in enumerate(PLAN["calls"], start=1):
    stray = {{"server": "relay", "tool": call["name"], "arguments": PLAN["stray_text"]}}
    item("item.started", "mcp_tool_call", **stray)
    _status, answer = rpc("tools/call", call, 10 + number, version)
    item("item.completed", "mcp_tool_call", **stray, result=PLAN["stray_text"])
item("item.completed", "agent_message", text="done")
emit({{"type": "turn.completed", "usage": {{}}}})
"""


def a_relay_calling_codex(
    directory: Path,
    *,
    version_line: str,
    calls: Sequence[ClaudeCall] = (),
    lists: str = "first",
    wrong_bearer: str = "",
    stray_text: str = "",
    timeout_seconds: float = 20.0,
) -> Path:
    """The pinned Codex's stand-in for a relay run; it captures into ``directory/capture``.

    ``lists`` is when it lists the relay's tools: ``first`` (before its first line),
    ``after-text`` (after a first message) or ``never`` (it answers without the relay, as a
    tool-free run must). With ``wrong_bearer`` it sends that instead of the secret and,
    refused, exits 1 with an empty stdout. Each call comes with Codex's own
    ``mcp_tool_call`` lines carrying ``stray_text``.
    """
    plan = {
        "version_line": version_line,
        "capture": str(directory / "capture"),
        "calls": [{"name": call.name, "arguments": dict(call.arguments)} for call in calls],
        "lists": lists,
        "wrong_bearer": wrong_bearer,
        "stray_text": stray_text,
        "timeout": timeout_seconds,
    }
    binary = directory / "codex"
    binary.write_text(_CODEX_SOURCE.format(python=sys.executable, plan=json.dumps(plan)))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


@dataclass(frozen=True)
class ChildCapture:
    """What the fake Claude recorded about its start and every reply the relay gave it."""

    directory: Path

    def text_of(self, name: str) -> str:
        return (self.directory / name).read_text() if (self.directory / name).exists() else ""

    @property
    def started(self) -> bool:
        return (self.directory / "argv").exists()

    @property
    def everything_it_was_given(self) -> str:
        files = self.directory / "files"
        file_texts = (
            [path.read_text() for path in sorted(files.iterdir())] if files.exists() else []
        )
        return "\n".join(
            [self.text_of("argv"), self.text_of("environment"), self.stdin, *file_texts]
        )

    @property
    def relay(self) -> dict[str, Any]:
        return json.loads(self.run_files["relay-mcp.json"])["mcpServers"]["relay"]

    @property
    def relay_secret(self) -> str:
        return self.relay["headers"]["Authorization"].removeprefix("Bearer ")

    @property
    def relay_port(self) -> int:
        return urlsplit(self.relay["url"]).port or 0

    @property
    def stdin(self) -> str:
        return self.text_of("stdin")

    @property
    def run_files(self) -> dict[str, str]:
        files = self.directory / "files"
        return {path.name: path.read_text() for path in files.iterdir()} if files.exists() else {}

    @property
    def replies(self) -> list[dict[str, Any]]:
        return self._replies_to("tools/call")

    @property
    def listing_replies(self) -> list[dict[str, Any]]:
        return self._replies_to("tools/list")

    def _replies_to(self, method: str) -> list[dict[str, Any]]:
        lines = self.text_of("replies").splitlines()
        return [reply for reply in map(json.loads, lines) if reply["method"] == method]


@dataclass(frozen=True)
class CodexChildCapture(ChildCapture):
    """What the fake Codex recorded; its relay entry is in its settings file."""

    @property
    def relay(self) -> dict[str, Any]:
        return tomllib.loads(self.run_files["config.toml"])["mcp_servers"]["relay"]

    @property
    def relay_port(self) -> int:
        return urlsplit(self.relay["url"]).port or 0

    @property
    def relay_secret(self) -> str:
        variable = f"{self.relay['bearer_token_env_var']}="
        lines = self.text_of("environment").splitlines()
        return next(line.removeprefix(variable) for line in lines if line.startswith(variable))


def relay_refuses(port: int, timeout_seconds: float) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout_seconds).close()
    except ConnectionRefusedError:
        return True
    return False


@dataclass(frozen=True)
class ToolApp:
    """The TLS fake app of a tool run: its origin, trust and what it was asked."""

    fake: McpAppFake

    @property
    def origin(self) -> str:
        return self.fake.origin

    @property
    def calls(self) -> list[RecordedRequest]:
        return [request for request in self.fake.requests if request.rpc_method == "tools/call"]

    @property
    def capability(self) -> str:
        bearer = self.fake.requests[0].headers["authorization"]
        return bearer.removeprefix("Bearer ")


@contextmanager
def a_tool_app(
    serving_authority: TestAuthority,
    tools: Sequence[str] = ("save_note", "read_note"),
    *,
    overrides: Mapping[str, Responder] | None = None,
    keep_alive: bool = False,
) -> Iterator[ToolApp]:
    app = ScriptedApp(pages=[tool_listing(list(tools))], overrides=overrides)
    credentials = serving_authority.credentials_for(CERTIFICATE_HOST)
    with serving(app, credentials, keep_alive=keep_alive) as fake:
        yield ToolApp(fake)
