"""A stand-in for the Grok CLI's streaming run, shared by the adapter's test modules."""

from __future__ import annotations

import json
import shlex
import stat
from pathlib import Path
from typing import Any

PRECONDITIONS_MARKER = "preconditions-passed"


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
    with ``capture`` it first records its arguments, the login and settings file it was given and
    a listing of its home.
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
