"""A release is dispatched; each gate step refuses before the tag step creates it."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
_REPOSITORY = "overnightworks/agent-runtime"
_VERSION = "0.3.0"
_OTHER_COMMIT = "f" * 40
_MIGRATION_LINK = (
    f"**Before you bump your pin,** read [Migrating to {_VERSION}]"
    f"(https://github.com/{_REPOSITORY}/blob/v{_VERSION}/docs/MIGRATING.md#migrating-to-030)."
)
_WHEELS = {
    "overnightworks_agent_runtime-{}-py3-none-any.whl": b"core",
    "overnightworks_agent_runtime_client-{}-py3-none-any.whl": b"client",
    "overnightworks_agent_runtime_service-{}-py3-none-any.whl": b"service",
}
_FIELD, _CALL = "\x1f", "\x1e"
# Stands in for `gh`: records every call and answers from fixtures, applying `--jq` like gh does.
_FAKE_GH = """#!/bin/bash
printf '%s\\037' "$@" >> "$FAKE_DIR/calls"
printf '\\036' >> "$FAKE_DIR/calls"
case "$*" in
  *commits/main*) fixture=main_head ;;
  *matching-refs*) fixture=refs ;;
  "run list"*) fixture=runs ;;
  "run view"*) fixture=jobs ;;
  *) exit 0 ;;
esac
filter=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--jq" ]; then filter="$2"; fi
  shift
done
if [ -n "$filter" ]; then jq -r "$filter" "$FAKE_DIR/$fixture"; else cat "$FAKE_DIR/$fixture"; fi
"""


def _run_script(step_name: str) -> str:
    step = re.search(
        rf"- name: {re.escape(step_name)}\n(?:.*\n)*?        run: \|\n((?:          .*\n|\n)+)",
        _WORKFLOW.read_text(encoding="utf-8"),
    )
    assert step, f"release.yml has no run script for {step_name}"
    return "\n".join(line[10:] for line in step.group(1).splitlines())


def _run_names() -> list[str]:
    text = _WORKFLOW.read_text(encoding="utf-8")
    return re.findall(r"- name: (.*)\n(?:.*\n)*?        run: \|", text)


def _write_fixtures(tmp_path: Path, **fixtures: object) -> None:
    directory = tmp_path / "fixtures"
    directory.mkdir(exist_ok=True)
    for name, value in fixtures.items():
        text = value if isinstance(value, str) else json.dumps(value)
        (directory / name).write_text(text, encoding="utf-8")


def _run_step(
    step_name: str,
    tmp_path: Path,
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
            "GITHUB_REPOSITORY": _REPOSITORY,
            "VERSION": _VERSION,
            "COMMIT": _OTHER_COMMIT,
            **(environment or {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(tmp_path: Path) -> list[list[str]]:
    recorded = (tmp_path / "fixtures" / "calls").read_text(encoding="utf-8")
    return [call.split(_FIELD)[:-1] for call in recorded.split(_CALL)[:-1]]


def _output_value(tmp_path: Path, name: str) -> str:
    text = (tmp_path / "github_output").read_text(encoding="utf-8")
    return text.removeprefix(f"{name}=").removesuffix("\n")


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


def _wheels_in(tmp_path: Path, version: str = _VERSION) -> dict[str, bytes]:
    wheels = {name.format(version): content for name, content in _WHEELS.items()}
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
def test_only_a_dispatch_from_main_passes_the_ref_gate(tmp_path, dispatch_ref, passes):
    result = _run_step("Refuse any ref but main", tmp_path, {"DISPATCH_REF": dispatch_ref})

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
def test_a_malformed_version_or_commit_is_refused(tmp_path, version, commit, line):
    result = _run_step(
        "Refuse a malformed version or commit",
        tmp_path,
        {"VERSION": version, "COMMIT": commit},
    )

    assert result.returncode == 1
    assert result.stderr.startswith(f"Refused: {line}")


def test_a_full_version_and_commit_pass_the_format_gate(tmp_path):
    result = _run_step("Refuse a malformed version or commit", tmp_path, {"COMMIT": "a" * 40})

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
    tmp_path, main_head, checked_out, line
):
    head = _commit_in(tmp_path)
    commits = {"same": head, "other": _OTHER_COMMIT}
    result = _run_step(
        "Refuse unless the commit is the checked-out head of main",
        tmp_path,
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
def test_a_version_whose_tag_exists_is_refused(tmp_path, refs, passes):
    result = _run_step("Refuse a tag that exists", tmp_path, refs=refs)

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
def test_the_commit_needs_a_green_push_run_with_a_green_quality_verdict(tmp_path, runs, jobs, line):
    result = _run_step(
        "Refuse unless CI's push run on the commit is green with the quality verdict",
        tmp_path,
        runs=runs,
        jobs={"jobs": [{"name": name, "conclusion": ok} for name, ok in jobs]},
    )

    assert result.returncode == (0 if line is None else 1)
    assert line is None or result.stderr.startswith("Refused:") and line in result.stderr


@pytest.mark.parametrize(
    ("built_version", "passes"),
    [(_VERSION, True), ("0.2.0", False)],
    ids=["at the version", "at another version"],
)
def test_the_build_must_hold_exactly_the_three_wheels_at_the_version(
    tmp_path, built_version, passes
):
    _wheels_in(tmp_path, built_version)

    result = _run_step(
        "Refuse unless the build holds exactly the three wheels at the version", tmp_path
    )

    assert (result.returncode == 0) is passes
    assert passes or result.stderr.startswith("Refused:")


def test_a_missing_or_extra_wheel_is_refused(tmp_path):
    wheels = _wheels_in(tmp_path)
    (tmp_path / "dist" / next(iter(wheels))).unlink()
    step = "Refuse unless the build holds exactly the three wheels at the version"

    missing = _run_step(step, tmp_path)
    _wheels_in(tmp_path)
    (tmp_path / "dist" / f"extra-{_VERSION}-py3-none-any.whl").touch()
    extra = _run_step(step, tmp_path)

    assert (missing.returncode, extra.returncode) == (1, 1)


def _notice_for(version: str, migrating: str, tmp_path: Path) -> str:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MIGRATING.md").write_text(migrating, encoding="utf-8")
    result = _run_step("Point a release at its migration section", tmp_path, {"VERSION": version})
    assert result.returncode == 0
    return _output_value(tmp_path, "notice")


def test_the_release_notice_links_the_migration_section_of_a_version_that_has_one(tmp_path):
    notice = _notice_for(_VERSION, f"# Title\n\n## Migrating to {_VERSION}\n\ntext\n", tmp_path)

    assert notice == _MIGRATION_LINK


@pytest.mark.parametrize(
    ("version", "heading"),
    [
        ("0.3.1", "## Migrating to 0.3.0"),
        (_VERSION, "## Install"),
        (_VERSION, "### Migrating to 0.3.0"),
    ],
    ids=["version of another section", "no section", "section one level down"],
)
def test_the_release_notice_is_empty_for_a_version_without_its_migration_section(
    tmp_path, version, heading
):
    assert _notice_for(version, f"# Title\n\n{heading}\n\ntext\n", tmp_path) == ""


def _wheel_list_output(tmp_path: Path) -> tuple[dict[str, bytes], str]:
    wheels = _wheels_in(tmp_path)
    result = _run_step("List the wheels with their hashes", tmp_path)
    assert result.returncode == 0
    return wheels, (tmp_path / "github_output").read_text(encoding="utf-8")


def test_the_release_page_lists_each_wheel_with_its_url_and_sha256(tmp_path):
    wheels, install = _wheel_list_output(tmp_path)
    for name, content in wheels.items():
        distribution = name.split("-")[0].replace("_", "-")
        url = f"https://github.com/{_REPOSITORY}/releases/download/v{_VERSION}/{name}"
        digest = hashlib.sha256(content).hexdigest()
        assert f"{distribution} @ {url}#sha256={digest}\n" in install


def test_the_wheel_list_is_written_in_the_multiline_output_form(tmp_path):
    _, install = _wheel_list_output(tmp_path)
    lines = install.splitlines()

    _, delimiter = lines[0].split("<<")
    assert lines[-1] == delimiter


_INSTALL_LINES = [
    f"overnightworks-agent-runtime{suffix} @ "
    f"https://example.test/overnightworks-agent-runtime{suffix}.whl#sha256={digest}"
    for suffix, digest in (("-client", "aa"), ("", "bb"), ("-service", "cc"))
]
_PUBLISH = "Publish the release on the tag with the wheels the gate built"
_TAG = "Create the tag at the gated commit"


def _tag_environment(notice: str = "") -> dict[str, str]:
    return {"NOTICE": notice, "INSTALL": "\n".join(_INSTALL_LINES)}


def _publish(notice: str, tmp_path: Path) -> list[str]:
    _wheels_in(tmp_path)
    result = _run_step(_PUBLISH, tmp_path, _tag_environment(notice))
    assert result.returncode == 0
    return _calls(tmp_path)[0]


def test_the_published_notes_open_with_the_migration_link_then_the_install_lines(tmp_path):
    notes = _publish(_MIGRATION_LINK, tmp_path)[-1]

    assert notes == "\n".join([_MIGRATION_LINK, "", *_INSTALL_LINES])


def test_the_published_notes_without_a_migration_section_are_the_install_lines_only(tmp_path):
    arguments = _publish("", tmp_path)

    assert arguments[-2:] == ["--notes", "\n".join(_INSTALL_LINES)]


def test_the_release_is_created_on_the_verified_tag_with_the_three_wheels(tmp_path):
    arguments = _publish("", tmp_path)

    assert arguments[:3] == ["release", "create", f"v{_VERSION}"]
    assert len([argument for argument in arguments if argument.startswith("dist/")]) == 3
    assert "--verify-tag" in arguments
    assert "--generate-notes" in arguments


def test_the_tag_is_created_at_the_gated_commit(tmp_path):
    result = _run_step(_TAG, tmp_path, {"COMMIT": "a" * 40}, refs=[])

    creations = [call for call in _calls(tmp_path) if "POST" in call]
    assert result.returncode == 0
    assert [f"ref=refs/tags/v{_VERSION}", f"sha={'a' * 40}"] == [
        argument for argument in creations[0] if argument.startswith(("ref=", "sha="))
    ]


def test_a_rerun_keeps_the_tag_that_already_points_at_the_commit(tmp_path):
    commit = "a" * 40
    refs = [{"ref": f"refs/tags/v{_VERSION}", "object": {"sha": commit}}]

    result = _run_step(_TAG, tmp_path, {"COMMIT": commit}, refs=refs)

    assert result.returncode == 0
    assert not [call for call in _calls(tmp_path) if "POST" in call]


def test_a_tag_at_another_commit_fails_the_job_without_creating_anything(tmp_path):
    refs = [{"ref": f"refs/tags/v{_VERSION}", "object": {"sha": _OTHER_COMMIT}}]

    result = _run_step(_TAG, tmp_path, {"COMMIT": "a" * 40}, refs=refs)

    assert result.returncode == 1
    assert result.stderr.startswith("Refused:")
    assert not [call for call in _calls(tmp_path) if "POST" in call]


@pytest.mark.parametrize("step_name", _run_names())
def test_a_release_script_never_interpolates_an_expression(step_name):
    assert "${{" not in _run_script(step_name)
