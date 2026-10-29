"""The CI test matrix and the quality verdict agree on which jobs are mandatory."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPOSITORY / ".github" / "workflows" / "ci.yml"


def _job_body(job: str) -> str:
    job_body = re.search(
        rf"^  {job}:\n(.*?)(?=^  \S|\Z)", _WORKFLOW.read_text(encoding="utf-8"), re.M | re.S
    )
    assert job_body, f"CI has no job {job}"
    return job_body.group(1)


def test_the_test_job_uses_the_matrix_for_the_python_version_only():
    test_job = _job_body("test")

    assert "matrix." not in re.sub(r"^\s+(python-version|if):.*$", "", test_job, flags=re.M)


def test_every_test_matrix_leg_is_a_mandatory_job_of_the_verdict():
    config = tomllib.loads((_REPOSITORY / "quality" / "config.toml").read_text(encoding="utf-8"))
    matrix = re.search(r'^        python-version: \[(.*)\]$', _job_body("test"), re.M)
    assert matrix
    legs = {f"test ({minor})" for minor in re.findall(r'"([\d.]+)"', matrix.group(1))}

    assert {job for job in config["gate"]["mandatory_jobs"] if job.startswith("test (")} == legs
