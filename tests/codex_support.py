"""A stand-in for the pinned Codex CLI, shared by the adapter's and the service's test modules."""

from __future__ import annotations

import shlex
import stat
from pathlib import Path

PRECONDITIONS_MARKER = "preconditions-passed"


def a_fake_codex(
    directory: Path,
    stream: tuple[bytes, ...],
    then: str = "exit 0",
    *,
    system_text: str,
    expected_stdin: str,
    version_line: str | None = None,
    capture: Path | None = None,
) -> Path:
    """A stand-in for the pinned CLI: a distinct exit code on a wrong run, else a marker file.

    A run checks its home, the blanked login, the system file, the stdin transcript and the
    isolation switch before it prints ``stream``; with ``capture`` it first records its
    arguments and the login it was given. ``version_line`` is what ``--version`` prints.
    """
    stream_file = directory / "stream.jsonl"
    stream_file.write_bytes(b"".join(stream))
    version_branch = (
        ""
        if version_line is None
        else f'[ "$1" = --version ] && {{ echo {shlex.quote(version_line)}; exit 0; }}\n'
    )
    record = (
        ""
        if capture is None
        else (
            f"mkdir -p {shlex.quote(str(capture))}\n"
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(capture / 'argv'))}\n"
            f'cp "$CODEX_HOME/auth.json" {shlex.quote(str(capture / "auth.json"))}\n'
        )
    )
    binary = directory / "codex"
    binary.write_text(
        "#!/bin/sh\n"
        f"{version_branch}"
        '[ "$CODEX_HOME" = "$HOME" ] || exit 10\n'
        '[ "$(stat -c %a "$CODEX_HOME/auth.json")" = 600 ] || exit 11\n'
        'grep -q \'"refresh_token": ""\' "$CODEX_HOME/auth.json" || exit 12\n'
        f'[ "$(cat "$CODEX_HOME/system-prompt")" = {shlex.quote(system_text)} ] || exit 13\n'
        f'[ "$(cat)" = {shlex.quote(expected_stdin)} ] || exit 14\n'
        'case " $* " in *" --ephemeral "*) ;; *) exit 15;; esac\n'
        f"{record}"
        f": > {shlex.quote(str(directory / PRECONDITIONS_MARKER))}\n"
        f"cat {shlex.quote(str(stream_file))}\n"
        f"{then}\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary
