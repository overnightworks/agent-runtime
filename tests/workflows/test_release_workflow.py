"""agent-runtime's own build step refuses all but the client wheel and lists it with its hash."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
_WITHIN_STEP = r"(?:(?!      - ).*\n)*?"


@dataclass(frozen=True)
class _Release:
    """The scenario every test of one release arranges: what is dispatched and what it builds."""

    version: str = "0.3.0"
    repository: str = "overnightworks/agent-runtime"
    wheel_contents: tuple[tuple[str, bytes], ...] = (
        ("overnightworks_agent_runtime_client-{}-py3-none-any.whl", b"client"),
    )


@pytest.fixture
def release() -> _Release:
    return _Release()


def _run_script(step_name: str) -> str:
    step = re.search(
        rf"- name: {re.escape(step_name)}\n{_WITHIN_STEP}        run: \|\n((?:          .*\n|\n)+)",
        _WORKFLOW.read_text(encoding="utf-8"),
    )
    assert step, f"release.yml has no run script for {step_name}"
    return "\n".join(line[10:] for line in step.group(1).splitlines())


def _run_step(
    step_name: str, tmp_path: Path, release: _Release
) -> subprocess.CompletedProcess[str]:
    """Run one step's script in `tmp_path`, with `tmp_path/bin` first on the path."""
    output = tmp_path / "github_output"
    output.touch()
    return subprocess.run(
        ["/bin/bash", "-e", "-c", _run_script(step_name)],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": release.repository,
            "VERSION": release.version,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _wheels_in(tmp_path: Path, release: _Release, version: str | None = None) -> dict[str, bytes]:
    wheels = {
        name.format(version or release.version): content for name, content in release.wheel_contents
    }
    (tmp_path / "dist").mkdir(exist_ok=True)
    for name, content in wheels.items():
        (tmp_path / "dist" / name).write_bytes(content)
    return wheels


@pytest.mark.parametrize(
    ("built_version", "passes"),
    [(None, True), ("0.2.0", False)],
    ids=["at the version", "at another version"],
)
def test_the_build_must_hold_exactly_the_client_wheel_at_the_version(
    tmp_path, release, built_version, passes
):
    _wheels_in(tmp_path, release, built_version)

    result = _run_step(
        "Refuse unless the build holds exactly the client wheel at the version",
        tmp_path,
        release,
    )

    assert (result.returncode == 0) is passes
    assert passes or result.stderr.startswith("Refused:")


def test_a_missing_or_extra_wheel_is_refused(tmp_path, release):
    wheels = _wheels_in(tmp_path, release)
    (tmp_path / "dist" / next(iter(wheels))).unlink()
    step = "Refuse unless the build holds exactly the client wheel at the version"

    missing = _run_step(step, tmp_path, release)
    _wheels_in(tmp_path, release)
    (tmp_path / "dist" / f"extra-{release.version}-py3-none-any.whl").touch()
    extra = _run_step(step, tmp_path, release)

    assert (missing.returncode, extra.returncode) == (1, 1)


def _wheel_list_output(tmp_path: Path, release: _Release) -> tuple[dict[str, bytes], str]:
    wheels = _wheels_in(tmp_path, release)
    result = _run_step("List the wheel with its hash", tmp_path, release)
    assert result.returncode == 0
    return wheels, (tmp_path / "github_output").read_text(encoding="utf-8")


def test_a_failing_sha256sum_fails_the_wheel_list_step(tmp_path, release):
    _wheels_in(tmp_path, release)
    failing = tmp_path / "bin" / "sha256sum"
    failing.parent.mkdir(exist_ok=True)
    failing.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    failing.chmod(0o755)

    result = _run_step("List the wheel with its hash", tmp_path, release)

    assert result.returncode != 0


def test_the_release_page_lists_each_wheel_with_its_url_and_sha256(tmp_path, release):
    wheels, install = _wheel_list_output(tmp_path, release)
    for name, content in wheels.items():
        distribution = name.split("-")[0].replace("_", "-")
        base = f"https://github.com/{release.repository}/releases/download"
        url = f"{base}/v{release.version}/{name}"
        digest = hashlib.sha256(content).hexdigest()
        assert f"{distribution} @ {url}#sha256={digest}\n" in install


def test_the_wheel_list_is_written_in_the_multiline_output_form(tmp_path, release):
    _, install = _wheel_list_output(tmp_path, release)
    lines = install.splitlines()

    _, delimiter = lines[0].split("<<")
    assert lines[-1] == delimiter


def test_a_release_run_value_never_interpolates_an_expression():
    run_values = re.findall(
        r"^ +run: (.*(?:\n(?: {10}.*|)$)*)", _WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE
    )

    assert run_values
    assert not [value for value in run_values if "${{" in value]
