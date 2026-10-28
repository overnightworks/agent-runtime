"""The service's Codex table: its config document, its login file and its stand-in program.

Shared by the service's Codex, startup, login and entrance test modules. Test values: the Codex
table with its login, the listed model ``gpt-5.5`` and the pinned version 0.163.0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import uvicorn
from pydantic import ValidationError
from tests.support.codex_support import DONE, THREAD, a_fake_codex, a_message
from tests.support.provider_test_support import (
    SYSTEM_TEXT,
    a_codex_login,
    a_fake_claude,
    a_private_root,
)
from tests.support.service_support import (
    LISTED_MODEL,
    PINNED_CLAUDE_VERSION,
    SONGMAKER_ORIGIN,
    TEST_ISSUER,
    _service_config_document,
    _toml_lines,
    process_token_file,
)

from agent_runtime_service.config import Provider, ServiceConfig

CODEX_MODEL = "gpt-5.5"


CODEX_PIN = "0.163.0"


CODEX_VERSION_LINE = f"codex-cli {CODEX_PIN}"


TURN_STDIN = "User: a\n\nAssistant: b\n\nUser: c"


ANSWER = (THREAD, a_message("done"), DONE)


CODEX_LOGIN_NAME = "codex-login.json"


@pytest.fixture(name="served_runs")
def the_served_runs(
    monkeypatch: pytest.MonkeyPatch, logging_state_restored: None
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: runs.append(where))
    return runs


def write_login(path: Path, contents: bytes, mode: int = 0o600) -> Path:
    """Write the login beside the file and move it over the file, as ``mv -T`` does."""
    beside = path.with_name(path.name + ".new")
    beside.write_bytes(contents)
    beside.chmod(mode)
    beside.replace(path)
    return path


def a_codex_login_file(directory: Path, contents: bytes | None = None) -> Path:
    return write_login(
        directory / CODEX_LOGIN_NAME, a_codex_login() if contents is None else contents
    )


def a_service_fake(directory: Path, stream: tuple[bytes, ...] = ANSWER, then: str = "exit 0", **kw):
    return a_fake_codex(
        directory,
        stream,
        then,
        system_text=SYSTEM_TEXT,
        expected_stdin=TURN_STDIN,
        version_line=CODEX_VERSION_LINE,
        **kw,
    )


def codex_document(
    tmp_path: Path,
    binary: Path,
    login_file: Path,
    *,
    runs: dict[str, object] | None = None,
    songmaker: dict[str, object] | None = None,
) -> dict[str, Any]:
    document = _service_config_document(TEST_ISSUER, runs, songmaker)
    if songmaker is None:
        del document["apps"]["songmaker"]["mcp_origin"]
    document["defaults"] = {"model": f"{Provider.OPENAI}/{CODEX_MODEL}"}
    document["cli"] = {
        "working_root": str(a_private_root(tmp_path)),
        Provider.OPENAI.value: {
            "binary": str(binary),
            "version": CODEX_PIN,
            "models": [CODEX_MODEL],
        },
    }
    document["cli"][Provider.OPENAI]["token_file"] = str(login_file)
    return document


def with_claude_beside(document: dict[str, Any], claude: Path) -> dict[str, Any]:
    document["cli"][Provider.ANTHROPIC] = {
        "binary": str(claude),
        "version": PINNED_CLAUDE_VERSION,
        "models": [LISTED_MODEL],
        "token_file": str(process_token_file()),
    }
    return document


def a_document_with_both(tmp_path: Path, stream: tuple[bytes, ...] = ANSWER) -> dict[str, Any]:
    """Claude and Codex tables, each with a stand-in program of its own under ``tmp_path``."""
    claude_directory = tmp_path / "claude-directory"
    claude_directory.mkdir()
    codex = codex_document(tmp_path, a_service_fake(tmp_path, stream), a_codex_login_file(tmp_path))
    return with_claude_beside(codex, a_fake_claude(claude_directory))


def a_codex_config(document: dict[str, Any]) -> ServiceConfig:
    return ServiceConfig.model_validate(document)


CODEX_NAME = f"{Provider.OPENAI}/{CODEX_MODEL}"


def a_config_with_both_providers(tmp_path: Path) -> ServiceConfig:
    """``service_config`` plus a listed ``[cli.openai]`` table."""
    document = a_document_with_both(tmp_path)
    document["defaults"] = {"model": f"{Provider.ANTHROPIC}/{LISTED_MODEL}"}
    document["apps"]["songmaker"]["mcp_origin"] = SONGMAKER_ORIGIN
    return a_codex_config(document)


def written_document(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "agent-runtime-service.toml"
    path.write_text("\n".join(_toml_lines(document)) + "\n", encoding="utf-8")
    return path


def the_refusal_of(document: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as refused:
        a_codex_config(document)
    return str(refused.value)
