"""The release build refuses all but the client wheel; its release note names the git tag."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
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
    commit: str = ""


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
            "COMMIT": release.commit,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _checkout_at_head(tmp_path: Path, release: _Release) -> _Release:
    """Make `tmp_path` a git checkout and return the scenario that gated its head commit."""
    git_env = {
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for command in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "head"]):
        subprocess.run(["git", *command], cwd=tmp_path, env=git_env, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=git_env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return replace(release, commit=head)


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


@pytest.mark.parametrize("gated_is_head", [True, False], ids=["the gated commit", "another commit"])
def test_the_build_must_check_out_the_commit_the_gate_judged(tmp_path, release, gated_is_head):
    checked_out = _checkout_at_head(tmp_path, release)
    gated = checked_out if gated_is_head else replace(checked_out, commit="0" * 40)

    result = _run_step("Refuse unless the checkout is the commit the gate judged", tmp_path, gated)

    assert (result.returncode == 0) is gated_is_head
    assert gated_is_head or (
        result.stderr
        == f"Refused: the checkout is {checked_out.commit}, not the gated commit {gated.commit}.\n"
    )


def test_a_missing_or_extra_wheel_is_refused(tmp_path, release):
    wheels = _wheels_in(tmp_path, release)
    (tmp_path / "dist" / next(iter(wheels))).unlink()
    step = "Refuse unless the build holds exactly the client wheel at the version"

    missing = _run_step(step, tmp_path, release)
    _wheels_in(tmp_path, release)
    (tmp_path / "dist" / f"extra-{release.version}-py3-none-any.whl").touch()
    extra = _run_step(step, tmp_path, release)

    assert (missing.returncode, extra.returncode) == (1, 1)


def _release_note(tmp_path: Path, release: _Release) -> str:
    result = _run_step("Write the release note naming the git tag", tmp_path, release)
    assert result.returncode == 0
    return (tmp_path / "github_output").read_text(encoding="utf-8")


def test_the_release_page_names_the_git_tag_requirement_and_no_wheel_url(tmp_path, release):
    note = _release_note(tmp_path, release)

    requirement = (
        "overnightworks-agent-runtime-client @ "
        f"git+https://github.com/{release.repository}@v{release.version}\n"
    )
    assert requirement in note
    assert "releases/download" not in note


def test_the_release_note_is_written_in_the_multiline_output_form(tmp_path, release):
    lines = _release_note(tmp_path, release).splitlines()

    _, delimiter = lines[0].split("<<")
    assert lines[-1] == delimiter


def test_a_release_run_value_never_interpolates_an_expression():
    run_values = re.findall(
        r"^ +run: (.*(?:\n(?: {10}.*|)$)*)", _WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE
    )

    assert run_values
    assert not [value for value in run_values if "${{" in value]
