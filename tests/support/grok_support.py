"""A stand-in for the Grok CLI's streaming run, shared by the adapter's test modules."""

from __future__ import annotations

import json
import shlex
import stat
from pathlib import Path
from typing import Any

from tests.support.provider_test_support import a_grok_login

from agent_runtime.cli_settings import CliSettings
from agent_runtime.grok.run import GrokRun
from agent_runtime.grok_login import assess_grok_login
from agent_runtime_client.wire import TurnMessage

PRECONDITIONS_MARKER = "preconditions-passed"


def a_grok_run(*settings: CliSettings) -> GrokRun:
    return GrokRun(
        Path("/opt/grok/bin/grok"),
        assess_grok_login(json.dumps(a_grok_login()).encode()),
        "system",
        (TurnMessage(role="user", text="question"),),
        settings=settings,
    )


def grok_line(**fields: Any) -> bytes:
    return (json.dumps(fields) + "\n").encode()


def a_text(data: str) -> bytes:
    return grok_line(type="text", data=data)


USAGE = grok_line(type="usage", input_tokens=1, output_tokens=1)
COMMANDS = grok_line(type="available_commands", tools=[], commands=[])
END = grok_line(type="end", stopReason="end_turn")


def a_fake_grok(
    directory: Path,
    stream: tuple[bytes, ...],
    then: str = "exit 0",
    *,
    system_text: str,
    expected_prompt: str,
    version_line: str | None = None,
    capture: Path | None = None,
) -> Path:
    """A stand-in for the CLI: a distinct exit code on a wrong run, else a marker file.

    A run checks its home, the blanked login, the rules file, the prompt file named by
    ``--prompt-file``, its empty stdin and its 0600 ``config.toml`` before it prints ``stream``;
    with ``capture`` it first records its arguments, its environment, the login and settings file
    it was given and a listing of its home.
    ``version_line`` is what ``--version`` prints.
    """
    (directory / "stream.jsonl").write_bytes(b"".join(stream))
    guards = (
        ('[ "$GROK_HOME" = "$HOME" ]', 10),
        ('[ "$(stat -c %a "$GROK_HOME/auth.json")" = 600 ]', 11),
        ('grep -q \'"refresh_token": ""\' "$GROK_HOME/auth.json"', 12),
        (f'[ "$(cat "$GROK_HOME/AGENTS.md")" = {shlex.quote(system_text)} ]', 13),
        (f'[ "$(cat "$prompt_file")" = {shlex.quote(expected_prompt)} ]', 14),
        ('[ -z "$(cat)" ]', 15),
        ('[ "$(stat -c %a "$GROK_HOME/config.toml")" = 600 ]', 16),
    )
    lines = [
        "#!/bin/sh",
        *(
            ()
            if version_line is None
            else (f'[ "$1" = --version ] && {{ echo {shlex.quote(version_line)}; exit 0; }}',)
        ),
        'for argument in "$@"; do [ "$previous" = --prompt-file ] && prompt_file=$argument;'
        " previous=$argument; done",
        *(f"{test} || exit {code}" for test, code in guards),
    ]
    if capture is not None:
        lines += [
            f"mkdir -p {shlex.quote(str(capture))}",
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(capture / 'argv'))}",
            f"env > {shlex.quote(str(capture / 'environment'))}",
            f'cp "$GROK_HOME/auth.json" {shlex.quote(str(capture / "auth.json"))}',
            f'cp "$GROK_HOME/config.toml" {shlex.quote(str(capture / "config.toml"))}',
            f'ls -A "$GROK_HOME" > {shlex.quote(str(capture / "home"))}',
        ]
    lines += [
        f": > {shlex.quote(str(directory / PRECONDITIONS_MARKER))}",
        f"cat {shlex.quote(str(directory / 'stream.jsonl'))}",
        then,
    ]
    binary = directory / "grok"
    binary.write_text("\n".join(lines) + "\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def a_renewing_fake_grok(
    directory: Path, login: bytes, renewed: bytes | None, *, threshold_at_least: int = 1
) -> Path:
    """A stand-in for the pinned CLI on a renewal run: it renews the login it was given.

    A run checks that ``grok models`` is its whole argv, its home, a 0600 login byte-equal to
    ``login``, an early-invalidation threshold of at least ``threshold_at_least``
    and its empty stdin, with a distinct exit code for each wrong one. Then it copies ``renewed``
    (if any) over the home's login.
    """
    expected = directory / "expected-login.json"
    expected.write_bytes(login)
    document = directory / "renewed-login.json"
    if renewed is not None:
        document.write_bytes(renewed)
    copy = f'cat {shlex.quote(str(document))} > "$GROK_HOME/auth.json"\n' if renewed else ""
    binary = directory / "grok"
    binary.write_text(
        "#!/bin/sh\n"
        '[ "$*" = models ] || exit 10\n'
        '[ "$GROK_HOME" = "$HOME" ] || exit 11\n'
        '[ "$(stat -c %a "$GROK_HOME/auth.json")" = 600 ] || exit 12\n'
        f'cmp -s {shlex.quote(str(expected))} "$GROK_HOME/auth.json" || exit 13\n'
        f'[ "${{GROK_AUTH_EARLY_INVALIDATION_SECS:-0}}" -ge {threshold_at_least} ] || exit 14\n'
        '[ -z "$(cat)" ] || exit 15\n'
        f"{copy}"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary
