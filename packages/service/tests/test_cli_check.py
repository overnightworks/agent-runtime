"""``--check`` runs every step a start runs before serving, then ends without serving."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.support.provider_test_support import fake_cli_executable
from tests.support.service_config_support import (
    MCP_SECTION,
    VALID_CONFIG,
    config_file_naming,
    the_working_root,  # noqa: F401 - an autouse fixture
    written,
)
from tests.support.service_renewal_support import (
    a_due_login_now,
    a_renewed_login_now,
    codex_service_files,
)
from tests.support.service_support import (
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
)
from tests.support.service_table_support import (
    the_served_runs,  # noqa: F401 - the fixture is named in its decorator
)

import agent_runtime_service.app as app_module
import agent_runtime_service.process_hardening as hardening
from agent_runtime_service.cli import EXIT_BAD_CONFIG, REFUSAL_PREFIX, main
from agent_runtime_service.renewal_schedule import RenewalSchedule


def _one_refusal_line(capsys) -> str:
    refusal = capsys.readouterr().err
    assert refusal.count("\n") == 1
    return refusal


def test_a_valid_file_passes_the_check_without_serving(tmp_path, served_runs):
    assert main(["--config", str(written(tmp_path, VALID_CONFIG)), "--check"]) == 0

    assert served_runs == []


def test_a_start_without_check_still_serves(tmp_path, served_runs):
    assert main(["--config", str(written(tmp_path, VALID_CONFIG))]) == 0

    assert len(served_runs) == 1


def _bad_field(tmp_path: Path, monkeypatch) -> Path:
    return written(tmp_path, VALID_CONFIG.replace("port = 8099", "port = 8099\nworkers = 4"))


def _wrong_pinned_version(tmp_path: Path, monkeypatch) -> Path:
    program = fake_cli_executable(tmp_path, {("--version",): "2.1.303 (Claude Code)\n"})
    return config_file_naming(tmp_path, program)


def _unloadable_mcp_bundle(tmp_path: Path, monkeypatch) -> Path:
    absent = tmp_path / "absent-ca.pem"
    text = VALID_CONFIG.replace(MCP_SECTION, f"[mcp]\nca_file = {json.dumps(str(absent))}\n\n")
    return written(tmp_path, text)


def _root_user(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(hardening, "running_platform", lambda: "linux")
    monkeypatch.setattr(hardening, "effective_user_id", lambda: 0)
    return written(tmp_path, VALID_CONFIG)


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
    tmp_path, capsys, served_runs, monkeypatch, working_root, configuration
):
    path = configuration(tmp_path, monkeypatch)

    assert main(["--config", str(path), "--check"]) == EXIT_BAD_CONFIG

    assert _one_refusal_line(capsys).startswith(REFUSAL_PREFIX)
    assert served_runs == []


def test_a_check_with_a_due_codex_login_starts_no_renewal(tmp_path, served_runs):
    files = codex_service_files(tmp_path, a_due_login_now(), a_renewed_login_now())

    assert main(["--config", str(files.configuration), "--check"]) == 0

    assert files.traces() == files.traces_without_a_renewal()


def test_a_start_hands_the_app_a_renewal_schedule_and_runs_none_before_the_app_serves(
    tmp_path, served_runs, monkeypatch
):
    files = codex_service_files(tmp_path, a_due_login_now(), a_renewed_login_now())
    created: list[dict[str, Any]] = []
    create_app = app_module.create_app

    def recording(*arguments: Any, **keywords: Any):
        created.append(keywords)
        return create_app(*arguments, **keywords)

    monkeypatch.setattr(app_module, "create_app", recording)

    assert main(["--config", str(files.configuration)]) == 0

    (keywords,) = created
    assert isinstance(keywords["renewals"], RenewalSchedule)
    assert files.traces() == files.traces_without_a_renewal()
