"""``--check`` runs every step a start runs before serving, then ends without serving."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import uvicorn
from tests.support.provider_test_support import fake_cli_executable
from tests.support.service_support import (
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
)
from tests.test_service_cli_version import PRINTS_THE_PIN, _config_file_naming, working_root  # noqa: F401
from tests.test_service_config import (
    MCP_SECTION,
    VALID_CONFIG,
    _written,
)

import agent_runtime_service.process_hardening as hardening
from agent_runtime_service.cli import EXIT_BAD_CONFIG, REFUSAL_PREFIX, main


@pytest.fixture
def served(monkeypatch, logging_state_restored):
    runs = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: runs.append(where))
    return runs


def _one_refusal_line(capsys) -> str:
    refusal = capsys.readouterr().err
    assert refusal.count("\n") == 1
    return refusal


def test_a_valid_file_passes_the_check_without_serving(tmp_path, served):
    assert main(["--config", str(_written(tmp_path, VALID_CONFIG)), "--check"]) == 0

    assert served == []


def test_a_start_without_check_still_serves(tmp_path, served):
    assert main(["--config", str(_written(tmp_path, VALID_CONFIG))]) == 0

    assert len(served) == 1


def _bad_field(tmp_path: Path, monkeypatch) -> Path:
    return _written(tmp_path, VALID_CONFIG.replace("port = 8099", "port = 8099\nworkers = 4"))


def _wrong_pinned_version(tmp_path: Path, monkeypatch) -> Path:
    program = fake_cli_executable(tmp_path, {("--version",): "2.1.303 (Claude Code)\n"})
    return _config_file_naming(tmp_path, program)


def _unloadable_mcp_bundle(tmp_path: Path, monkeypatch) -> Path:
    absent = tmp_path / "absent-ca.pem"
    text = VALID_CONFIG.replace(MCP_SECTION, f"[mcp]\nca_file = {json.dumps(str(absent))}\n\n")
    return _written(tmp_path, text)


def _root_user(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(hardening, "running_platform", lambda: "linux")
    monkeypatch.setattr(hardening, "effective_user_id", lambda: 0)
    return _written(tmp_path, VALID_CONFIG)


@pytest.mark.parametrize(
    "configuration",
    [
        pytest.param(_bad_field, id="bad-field"),
        pytest.param(_wrong_pinned_version, id="wrong-pinned-version"),
        pytest.param(_unloadable_mcp_bundle, id="unloadable-mcp-ca-bundle"),
        pytest.param(_root_user, id="root-user"),
    ],
)
def test_a_check_refuses_with_the_starts_one_line_and_serves_nothing(
    tmp_path, capsys, served, monkeypatch, working_root, configuration  # noqa: F811
):
    path = configuration(tmp_path, monkeypatch)

    assert main(["--config", str(path), "--check"]) == EXIT_BAD_CONFIG

    assert _one_refusal_line(capsys).startswith(REFUSAL_PREFIX)
    assert served == []
