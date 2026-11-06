"""A release is dispatched; each gate step refuses before the tag step creates it."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
_WITHIN_STEP = r"(?:(?!      - ).*\n)*?"
_FIELD, _CALL = "\x1f", "\x1e"


@dataclass(frozen=True)
class _Release:
    """The scenario every test of one release arranges: what is dispatched and what it builds."""

    version: str = "0.3.0"
    other_commit: str = "f" * 40
    repository: str = "overnightworks/agent-runtime"
    wheel_contents: tuple[tuple[str, bytes], ...] = (
        ("overnightworks_agent_runtime_client-{}-py3-none-any.whl", b"client"),
    )

    @property
    def install_lines(self) -> list[str]:
        return [
            "overnightworks-agent-runtime-client @ "
            "https://example.test/overnightworks-agent-runtime-client.whl#sha256=aa"
        ]


@pytest.fixture
def release() -> _Release:
    return _Release()


# Stands in for `gh`: records every call and answers from fixtures, applying `--jq` like gh does.
_FAKE_GH = """#!/bin/bash
printf '%s\\037' "$@" >> "$FAKE_DIR/calls"
printf '\\036' >> "$FAKE_DIR/calls"
case "$*" in
  *commits/main*) fixture=main_head ;;
  *matching-refs*) fixture=refs ;;
  "run list"*)
    fixture=empty
    if [[ "$*" == *"--workflow ci.yml"* && "$*" == *"--event push"* \\
      && "$*" == *"--commit $COMMIT"* ]]; then
      fixture=runs
    fi ;;
  "run view"*) fixture="jobs_$3" ;;
  "release create"*) if [ -f "$FAKE_DIR/release_fails" ]; then exit 1; fi; exit 0 ;;
  *) exit 0 ;;
esac
filter=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--jq" ]; then filter="$2"; fi
  shift
done
[ -f "$FAKE_DIR/$fixture" ] || exit 1
if [ -n "$filter" ]; then jq -r "$filter" "$FAKE_DIR/$fixture"; else cat "$FAKE_DIR/$fixture"; fi
"""


def _run_script(step_name: str) -> str:
    step = re.search(
        rf"- name: {re.escape(step_name)}\n{_WITHIN_STEP}        run: \|\n((?:          .*\n|\n)+)",
        _WORKFLOW.read_text(encoding="utf-8"),
    )
    assert step, f"release.yml has no run script for {step_name}"
    return "\n".join(line[10:] for line in step.group(1).splitlines())


def _run_names() -> list[str]:
    text = _WORKFLOW.read_text(encoding="utf-8")
    return re.findall(rf"- name: (.*)\n{_WITHIN_STEP}        run: \|", text)


def _write_fixtures(tmp_path: Path, **fixtures: object) -> None:
    directory = tmp_path / "fixtures"
    directory.mkdir(exist_ok=True)
    (directory / "empty").write_text("[]", encoding="utf-8")
    for name, value in fixtures.items():
        text = value if isinstance(value, str) else json.dumps(value)
        (directory / name).write_text(text, encoding="utf-8")


def _run_step(
    step_name: str,
    tmp_path: Path,
    release: _Release,
    environment: dict[str, str] | None = None,
    **fixtures: object,
) -> subprocess.CompletedProcess[str]:
    """Run one step's script in `tmp_path` with a fake `gh` first on the path."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    fake_gh = bin_directory / "gh"
    fake_gh.write_text(_FAKE_GH, encoding="utf-8")
    fake_gh.chmod(0o755)
    _write_fixtures(tmp_path, **fixtures)
    output = tmp_path / "github_output"
    output.touch()
    return subprocess.run(
        ["/bin/bash", "-e", "-c", _run_script(step_name)],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_directory}:/usr/bin:/bin",
            "FAKE_DIR": str(tmp_path / "fixtures"),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": release.repository,
            "VERSION": release.version,
            "COMMIT": release.other_commit,
            **(environment or {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(tmp_path: Path) -> list[list[str]]:
    recorded = (tmp_path / "fixtures" / "calls").read_text(encoding="utf-8")
    return [call.split(_FIELD)[:-1] for call in recorded.split(_CALL)[:-1]]


def _commit_in(tmp_path: Path) -> str:
    identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    environment = {**identity, "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    for arguments in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "head"]):
        subprocess.run(["git", *arguments], cwd=tmp_path, env=environment, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _wheels_in(tmp_path: Path, release: _Release, version: str | None = None) -> dict[str, bytes]:
    wheels = {
        name.format(version or release.version): content for name, content in release.wheel_contents
    }
    (tmp_path / "dist").mkdir(exist_ok=True)
    for name, content in wheels.items():
        (tmp_path / "dist" / name).write_bytes(content)
    return wheels


@pytest.mark.parametrize(
    ("dispatch_ref", "passes"),
    [
        ("refs/heads/main", True),
        ("refs/heads/feature", False),
        ("refs/tags/v0.3.0", False),
    ],
    ids=["main", "another branch", "a tag"],
)
def test_only_a_dispatch_from_main_passes_the_ref_gate(tmp_path, release, dispatch_ref, passes):
    result = _run_step("Refuse any ref but main", tmp_path, release, {"DISPATCH_REF": dispatch_ref})

    assert (result.returncode == 0) is passes
    assert passes or result.stderr.startswith("Refused: dispatched from")


@pytest.mark.parametrize(
    ("version", "commit", "line"),
    [
        ("0.21", "a" * 40, "the version"),
        ("v0.21.0", "a" * 40, "the version"),
        ("0.21.0-rc1", "a" * 40, "the version"),
        ("0.1.0; rm -rf", "a" * 40, "the version"),
        ("0.21.0", "a" * 39, "the commit"),
        ("0.21.0", "A" * 40, "the commit"),
        ("0.21.0", "g" * 40, "the commit"),
    ],
)
def test_a_malformed_version_or_commit_is_refused(tmp_path, release, version, commit, line):
    result = _run_step(
        "Refuse a malformed version or commit",
        tmp_path,
        release,
        {"VERSION": version, "COMMIT": commit},
    )

    assert result.returncode == 1
    assert result.stderr.startswith(f"Refused: {line}")


def test_a_full_version_and_commit_pass_the_format_gate(tmp_path, release):
    result = _run_step(
        "Refuse a malformed version or commit", tmp_path, release, {"COMMIT": "a" * 40}
    )

    assert result.returncode == 0


@pytest.mark.parametrize(
    ("main_head", "checked_out", "line"),
    [
        ("same", "same", None),
        ("same", "other", "the commit is not the one checked out"),
        ("other", "same", "the commit is not main's head"),
    ],
    ids=["pass", "checkout moved", "main moved"],
)
def test_the_commit_must_be_the_checked_out_head_and_main_s_head(
    tmp_path, release, main_head, checked_out, line
):
    head = _commit_in(tmp_path)
    commits = {"same": head, "other": release.other_commit}
    result = _run_step(
        "Refuse unless the commit is the checked-out head of main",
        tmp_path,
        release,
        {"COMMIT": commits[checked_out]},
        main_head=json.dumps({"sha": commits[main_head]}),
    )

    assert result.returncode == (0 if line is None else 1)
    assert line is None or result.stderr.startswith(f"Refused: {line}")


@pytest.mark.parametrize(
    ("refs", "passes"),
    [
        ([], True),
        ([{"ref": "refs/tags/v0.3.01"}], True),
        ([{"ref": "refs/tags/v0.3.0"}], False),
    ],
    ids=["no tag", "a longer tag name", "the tag exists"],
)
def test_a_version_whose_tag_exists_is_refused(tmp_path, release, refs, passes):
    result = _run_step("Refuse a tag that exists", tmp_path, release, refs=refs)

    assert (result.returncode == 0) is passes
    assert passes or result.stderr.startswith("Refused: the tag v0.3.0 exists")


@pytest.mark.parametrize(
    ("runs", "jobs", "line"),
    [
        ([{"databaseId": 7, "conclusion": "success"}], [("quality / verdict", "success")], None),
        (
            [
                {"databaseId": 8, "conclusion": "failure"},
                {"databaseId": 7, "conclusion": "success"},
            ],
            [("quality / verdict", "success")],
            None,
        ),
        ([], [], "CI has no green push run"),
        ([{"databaseId": 7, "conclusion": "failure"}], [], "CI has no green push run"),
        ([{"databaseId": 7, "conclusion": "success"}], [("lint", "success")], "run 7 has no green"),
        (
            [{"databaseId": 7, "conclusion": "success"}],
            [("quality / verdict", "failure")],
            "run 7 has no green",
        ),
    ],
    ids=[
        "green with verdict",
        "an earlier green run",
        "no run",
        "red run",
        "green without verdict",
        "red verdict",
    ],
)
def test_the_commit_needs_a_green_push_run_with_a_green_quality_verdict(
    tmp_path, release, runs, jobs, line
):
    # The fake answers a run list only for CI's push runs on COMMIT, and a run view only by its id.
    result = _run_step(
        "Refuse unless CI's push run on the commit is green with the quality verdict",
        tmp_path,
        release,
        runs=runs,
        jobs_7={"jobs": [{"name": name, "conclusion": ok} for name, ok in jobs]},
    )

    assert result.returncode == (0 if line is None else 1)
    assert line is None or result.stderr.startswith("Refused:") and line in result.stderr


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
    result = _run_step("List the wheels with their hashes", tmp_path, release)
    assert result.returncode == 0
    return wheels, (tmp_path / "github_output").read_text(encoding="utf-8")


def test_a_failing_sha256sum_fails_the_wheel_list_step(tmp_path, release):
    _wheels_in(tmp_path, release)
    failing = tmp_path / "bin" / "sha256sum"
    failing.parent.mkdir(exist_ok=True)
    failing.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    failing.chmod(0o755)

    result = _run_step("List the wheels with their hashes", tmp_path, release)

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


_PUBLISH = "Publish the release on the tag with the wheels the gate built"
_TAG = "Create the tag at the gated commit"


def _tag_environment(release: _Release) -> dict[str, str]:
    return {"INSTALL": "\n".join(release.install_lines)}


def _publish(tmp_path: Path, release: _Release) -> list[str]:
    _wheels_in(tmp_path, release)
    result = _run_step(_PUBLISH, tmp_path, release, _tag_environment(release))
    assert result.returncode == 0
    return _calls(tmp_path)[0]


def test_the_published_notes_are_the_install_lines(tmp_path, release):
    arguments = _publish(tmp_path, release)

    assert arguments[-2:] == ["--notes", "\n".join(release.install_lines)]


def test_the_release_is_created_on_the_verified_tag_with_the_client_wheel(tmp_path, release):
    arguments = _publish(tmp_path, release)

    assert arguments[:3] == ["release", "create", f"v{release.version}"]
    assert len([argument for argument in arguments if argument.startswith("dist/")]) == 1
    assert "--verify-tag" in arguments
    assert "--generate-notes" in arguments


def _tag_creations(tmp_path: Path) -> list[list[str]]:
    return [call for call in _calls(tmp_path) if "POST" in call]


def test_the_tag_is_created_at_the_gated_commit(tmp_path, release):
    result = _run_step(_TAG, tmp_path, release, {"COMMIT": "a" * 40}, refs=[])

    creations = _tag_creations(tmp_path)
    assert result.returncode == 0
    assert [argument for argument in creations[0] if argument.startswith(("ref=", "sha="))] == [
        f"ref=refs/tags/v{release.version}",
        f"sha={'a' * 40}",
    ]


def test_a_rerun_keeps_the_tag_that_already_points_at_the_commit(tmp_path, release):
    commit = "a" * 40
    refs = [{"ref": f"refs/tags/v{release.version}", "object": {"sha": commit}}]

    result = _run_step(_TAG, tmp_path, release, {"COMMIT": commit}, refs=refs)

    assert result.returncode == 0
    assert not _tag_creations(tmp_path)


def test_a_tag_at_another_commit_fails_the_job_without_creating_anything(tmp_path, release):
    refs = [{"ref": f"refs/tags/v{release.version}", "object": {"sha": release.other_commit}}]

    result = _run_step(_TAG, tmp_path, release, {"COMMIT": "a" * 40}, refs=refs)

    assert result.returncode == 1
    assert result.stderr.startswith("Refused:")
    assert not _tag_creations(tmp_path)


def _operations(tmp_path: Path) -> list[str]:
    names = {"POST": "create the tag", "release": "create the release"}
    return [
        names["POST" if "POST" in call else call[0]]
        for call in _calls(tmp_path)
        if "POST" in call or call[:2] == ["release", "create"]
    ]


def test_the_tag_is_created_before_the_release_and_a_failed_publication_is_retried_on_it(
    tmp_path, release
):
    commit = "a" * 40
    environment = {"COMMIT": commit, **_tag_environment(release)}
    _wheels_in(tmp_path, release)

    assert _run_names().index(_TAG) < _run_names().index(_PUBLISH)
    tagged = _run_step(_TAG, tmp_path, release, environment, refs=[])
    published = _run_step(_PUBLISH, tmp_path, release, environment, release_fails="")
    (tmp_path / "fixtures" / "release_fails").unlink()
    created = [{"ref": f"refs/tags/v{release.version}", "object": {"sha": commit}}]
    retagged = _run_step(_TAG, tmp_path, release, environment, refs=created)
    republished = _run_step(_PUBLISH, tmp_path, release, environment)

    assert (tagged.returncode, published.returncode) == (0, 1)
    assert (retagged.returncode, republished.returncode) == (0, 0)
    assert _operations(tmp_path) == [
        "create the tag",
        "create the release",
        "create the release",
    ]


def test_a_release_run_value_never_interpolates_an_expression():
    run_values = re.findall(
        r"^ +run: (.*(?:\n(?: {10}.*|)$)*)", _WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE
    )

    assert run_values
    assert not [value for value in run_values if "${{" in value]
