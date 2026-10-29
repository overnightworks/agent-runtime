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
    capture: Path | None = None,
) -> Path:
    """A stand-in for the CLI: a distinct exit code on a wrong run, else a marker file.

    A run checks its home, the blanked login, the rules file, the prompt file named by
    ``--prompt-file`` and its empty stdin before it prints ``stream``; with ``capture`` it
    first records its arguments and the login it was given.
    """
    stream_file = directory / "stream.jsonl"
    stream_file.write_bytes(b"".join(stream))
    record = (
        ""
        if capture is None
        else (
            f"mkdir -p {shlex.quote(str(capture))}\n"
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(capture / 'argv'))}\n"
            f'cp "$GROK_HOME/auth.json" {shlex.quote(str(capture / "auth.json"))}\n'
        )
    )
    binary = directory / "grok"
    binary.write_text(
        "#!/bin/sh\n"
        '[ "$GROK_HOME" = "$HOME" ] || exit 10\n'
        '[ "$(stat -c %a "$GROK_HOME/auth.json")" = 600 ] || exit 11\n'
        'grep -q \'"refresh_token": ""\' "$GROK_HOME/auth.json" || exit 12\n'
        f'[ "$(cat "$GROK_HOME/AGENTS.md")" = {shlex.quote(system_text)} ] || exit 13\n'
        'prompt_file=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  [ "$1" = --prompt-file ] && prompt_file="$2"\n'
        "  shift\n"
        "done\n"
        f'[ "$(cat "$prompt_file")" = {shlex.quote(expected_prompt)} ] || exit 14\n'
        '[ -z "$(cat)" ] || exit 15\n'
        f"{record}"
        f": > {shlex.quote(str(directory / PRECONDITIONS_MARKER))}\n"
        f"cat {shlex.quote(str(stream_file))}\n"
        f"{then}\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary
