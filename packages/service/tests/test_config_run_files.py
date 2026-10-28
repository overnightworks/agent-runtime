"""The ``[runs]`` limit on the files of one run."""

from __future__ import annotations

import pytest
from tests.support.service_config_support import (
    VALID_CONFIG,
    assert_startup_refuses,
    written,
)

from agent_runtime_client.wire import MAX_RUN_FILES_BYTES
from agent_runtime_service.cli import EXIT_BAD_CONFIG, main
from agent_runtime_service.config import load_config

FIELD = "runs.max_files_bytes"


def _with_runs(setting: str) -> str:
    return VALID_CONFIG + f"\n[runs]\n{setting}\n"


def test_a_run_files_limit_left_out_defaults_to_eight_mebibytes(tmp_path):
    assert load_config(written(tmp_path, VALID_CONFIG)).runs.max_files_bytes == 8_388_608


@pytest.mark.parametrize("limit", [1, MAX_RUN_FILES_BYTES])
def test_a_run_files_limit_within_the_wire_ceiling_loads(tmp_path, limit):
    runs = load_config(written(tmp_path, _with_runs(f"max_files_bytes = {limit}"))).runs

    assert runs.max_files_bytes == limit


@pytest.mark.parametrize(
    "setting",
    [
        f"max_files_bytes = {MAX_RUN_FILES_BYTES + 1}",
        "max_files_bytes = 0",
        "max_files_bytes = -1",
        "max_files_bytes = true",
        "max_files_bytes = 8388608.0",
        'max_files_bytes = "8"',
    ],
)
def test_a_run_files_limit_the_wire_cannot_carry_stops_startup_naming_the_field(
    tmp_path, capsys, setting
):
    assert_startup_refuses(tmp_path, capsys, _with_runs(setting), FIELD)


def test_the_check_refuses_a_run_files_limit_above_the_wire_ceiling_as_a_start_does(
    tmp_path, capsys
):
    path = written(tmp_path, _with_runs(f"max_files_bytes = {MAX_RUN_FILES_BYTES + 1}"))

    status = main(["--config", str(path), "--check"])

    assert (status, FIELD in capsys.readouterr().err) == (EXIT_BAD_CONFIG, True)
