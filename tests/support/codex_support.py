"""A stand-in for the pinned Codex CLI, shared by the adapter's and the service's test modules."""

from __future__ import annotations

import base64
import json
import shlex
import stat
from pathlib import Path
from typing import Any

PRECONDITIONS_MARKER = "preconditions-passed"
FEATURES_OFF = (
    "shell_tool",
    "unified_exec",
    "browser_use",
    "computer_use",
    "multi_agent",
    "image_generation",
    "plugins",
    "hooks",
    "code_mode_host",
    "code_mode",
    "code_mode_only",
)
DEFAULT_SETTINGS: dict[str, Any] = {
    "sandbox_mode": "read-only",
    "approval_policy": "never",
    "mcp_servers": {},
    "web_search": "disabled",
    "features": dict.fromkeys(FEATURES_OFF, False),
}


def codex_line(**fields: Any) -> bytes:
    return (json.dumps(fields) + "\n").encode()


def an_item(event_type: str, item_type: str, **item: Any) -> bytes:
    return codex_line(type=event_type, item={"id": "item_0", "type": item_type, **item})


def a_message(text: str) -> bytes:
    return an_item("item.completed", "agent_message", text=text)


THREAD = codex_line(type="thread.started", thread_id="thread")
DONE = codex_line(type="turn.completed", usage={})


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

    A run checks its home, the blanked login, the system file, its settings file, the stdin
    transcript and the ephemeral flag before it prints ``stream``; with ``capture`` it first
    records its arguments, the login and the settings file it was given. ``version_line`` is
    what ``--version`` prints.
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
            f'cp "$CODEX_HOME/config.toml" {shlex.quote(str(capture / "config.toml"))}\n'
        )
    )
    binary = directory / "codex"
    binary.write_text(
        "#!/bin/sh\n"
        f"{version_branch}"
        '[ "$CODEX_HOME" = "$HOME" ] || exit 10\n'
        '[ "$(stat -c %a "$CODEX_HOME/auth.json")" = 600 ] || exit 11\n'
        'grep -q \'"refresh_token": ""\' "$CODEX_HOME/auth.json" || exit 12\n'
        '[ "$(stat -c %a "$CODEX_HOME/config.toml")" = 600 ] || exit 16\n'
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


def a_codex_access_token(payload: dict[str, Any] | bytes) -> str:
    """A JWT-shaped access token whose claims are ``payload``; the signature is not a signature."""
    claims = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    segments = (b'{"alg":"none"}', claims, b"signature")
    return ".".join(base64.urlsafe_b64encode(part).rstrip(b"=").decode() for part in segments)


def a_renewing_fake_codex(
    directory: Path,
    renewed: bytes | None,
    then: str = "exit 0",
    *,
    version_line: str | None = None,
) -> Path:
    """A stand-in for the pinned CLI on a renewal run: it renews the login it was given.

    A run checks its home, a full login with an emptied access token and a non-empty refresh
    token, the file credential store, the fixed stdin and the ephemeral flag, with a distinct
    exit code for each wrong one. Then it copies ``renewed`` (if any) over the home's login and
    runs ``then``. ``version_line`` is what ``--version`` prints.
    """
    version_branch = (
        ""
        if version_line is None
        else f'[ "$1" = --version ] && {{ echo {shlex.quote(version_line)}; exit 0; }}\n'
    )
    document = directory / "renewed-login.json"
    if renewed is not None:
        document.write_bytes(renewed)
    copy = f'cat {shlex.quote(str(document))} > "$CODEX_HOME/auth.json"\n' if renewed else ""
    binary = directory / "codex"
    binary.write_text(
        "#!/bin/sh\n"
        f"{version_branch}"
        '[ "$CODEX_HOME" = "$HOME" ] || exit 10\n'
        '[ "$(stat -c %a "$CODEX_HOME/auth.json")" = 600 ] || exit 11\n'
        'grep -q \'"access_token": ""\' "$CODEX_HOME/auth.json" || exit 12\n'
        'grep -q \'"refresh_token": ""\' "$CODEX_HOME/auth.json" && exit 13\n'
        'grep -q \'cli_auth_credentials_store = "file"\' "$CODEX_HOME/config.toml" || exit 14\n'
        "[ \"$(cat)\" = 'User: Say ok.' ] || exit 15\n"
        'case " $* " in *" --ephemeral "*) ;; *) exit 16;; esac\n'
        f"{copy}"
        f"{then}\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary
