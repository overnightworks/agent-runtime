"""The service configuration document the service tests start from, and how they write it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.api_support import API_SECTION
from tests.support.mcp_app_fake import TestAuthority
from tests.support.private_removal_support import remove_the_leftover
from tests.support.provider_test_support import fake_cli_executable
from tests.support.service_support import (
    PINNED_CLAUDE_VERSION,
    process_claude_cli,
    process_signing_key_file,
    process_token_file,
    process_working_root,
)

from agent_runtime_service.cli import EXIT_BAD_CONFIG, main
from agent_runtime_service.config import Provider

SONGMAKER_ORIGIN_LINE = 'mcp_origin = "https://songmaker.internal"\n'
SIGNING_KEY_LINE = f"key_file = {json.dumps(str(process_signing_key_file()))}"
SIGNING_SECTION = f"[signing]\n{SIGNING_KEY_LINE}\n\n"
MCP_CA_FILE = TestAuthority(process_signing_key_file().parent).ca_file
MCP_SECTION = f"[mcp]\nca_file = {json.dumps(str(MCP_CA_FILE))}\n\n"
TOKEN_FILE_LINE = f"token_file = {json.dumps(str(process_token_file()))}"
CLAUDE_BINARY_LINE = f"binary = {json.dumps(str(process_claude_cli()))}"
CLAUDE_VERSION_LINE = f'version = "{PINNED_CLAUDE_VERSION}"'
WORKING_ROOT_LINE = f"working_root = {json.dumps(str(process_working_root().path))}"

MODELS_LINE = 'models = ["sonnet", "opus"]'


def cli_section(
    *, binary: str, version: str, root: str, models: str = MODELS_LINE, token: str = TOKEN_FILE_LINE
) -> str:
    root_lines = f"{root}\n" if root else ""
    program_lines = "".join(f"{line}\n" for line in (binary, version, models, token) if line)
    program_table = f"[cli.{Provider.ANTHROPIC}]\n{program_lines}\n" if program_lines else ""
    return f"[cli]\n{root_lines}\n{program_table}"


CLI_SECTION = cli_section(
    binary=CLAUDE_BINARY_LINE, version=CLAUDE_VERSION_LINE, root=WORKING_ROOT_LINE
)

SUBSCRIPTION_GROUPS_LINE = (
    'subscription_groups = ["/old-subscription-members", "/subscription-members"]'
)

VALID_CONFIG = f"""
defaults = {{ model = "{Provider.ANTHROPIC}/sonnet" }}
[identity]
issuer = "https://keycloak.test/realms/agent-test"
jwks_url = "https://keycloak.test/realms/agent-test/protocol/openid-connect/certs"
audience = "agent-test-audience"
{SUBSCRIPTION_GROUPS_LINE}
api_groups = ["/api-members"]
group_claim = "member-groups"

[server]
host = "127.0.0.1"
port = 8099

{CLI_SECTION}{SIGNING_SECTION}{MCP_SECTION}{API_SECTION}
[apps.songmaker]
workload_client = "songmaker-test"
{SONGMAKER_ORIGIN_LINE}"""

PRINTS_THE_PIN = f"{PINNED_CLAUDE_VERSION} (Claude Code)\n"
WORKING_ROOT_NAME = "working-root"


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agent-runtime-service.toml"
    path.write_text(text, encoding="utf-8")
    return path


def assert_startup_refuses(tmp_path, capsys, text, named_field):
    assert main(["--config", str(written(tmp_path, text))]) == EXIT_BAD_CONFIG
    assert named_field in capsys.readouterr().err


def refused_on_one_line(path: Path, capsys) -> str:
    assert main(["--config", str(path)]) == EXIT_BAD_CONFIG
    refusal = capsys.readouterr().err
    assert refusal.count("\n") == 1
    return refusal.removeprefix(f"agent-runtime-service: refusing to start: {path}: ").rstrip("\n")


def working_root_of(tmp_path: Path) -> Path:
    root = tmp_path / WORKING_ROOT_NAME
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    return root


@pytest.fixture(name="working_root", autouse=True)
def the_working_root(tmp_path: Path) -> Iterator[Path]:
    root = working_root_of(tmp_path)
    yield root
    for leftover in root.iterdir():
        remove_the_leftover(leftover)


def printing(directory: Path, stdout: str) -> Path:
    return fake_cli_executable(directory, {("--version",): stdout})


def document_naming(program: Path, root: Path, base: str = VALID_CONFIG) -> str:
    binary_line = f"binary = {json.dumps(str(program))}"
    root_line = f"working_root = {json.dumps(str(root))}"
    return base.replace(
        CLI_SECTION,
        cli_section(binary=binary_line, version=CLAUDE_VERSION_LINE, root=root_line),
    )


def config_file_naming(tmp_path: Path, program: Path, base: str = VALID_CONFIG) -> Path:
    return written(tmp_path, document_naming(program, working_root_of(tmp_path), base))


def logged_program(tmp_path: Path, probe_log: Path) -> Path:
    return fake_cli_executable(tmp_path, {("--version",): PRINTS_THE_PIN}, calls_log=probe_log)
