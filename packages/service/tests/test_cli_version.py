"""The service starts only when each pinned program reports exactly the version the config pins."""

from __future__ import annotations

import errno
import fnmatch
import os
import stat
import tempfile
import threading
from pathlib import Path

import pytest
from tests.support import bounds, provider_test_support
from tests.support.private_removal_support import REMOVAL_FAILED, rmtree_refused
from tests.support.provider_test_support import (
    Hold,
    RunnerFailureWatch,
    fail_the_directory_named,
    fake_cli_executable,
    fake_cli_process,
    install_cli_door,
    removal_errors,
    the_only_tree,
)
from tests.support.service_codex_support import (
    a_codex_login_file,
    the_served_runs,  # noqa: F401 - the fixture is named in its decorator
)
from tests.support.service_config_support import (
    PRINTS_THE_PIN,
    VALID_CONFIG,
    config_file_naming,
    document_naming,
    printing,
    refused_on_one_line,
    the_working_root,  # noqa: F401 - an autouse fixture
    working_root_of,
    written,
)
from tests.support.service_support import (
    PINNED_CLAUDE_VERSION,
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    process_token_file,
)

import agent_runtime.process as core_process
from agent_runtime.config import reset_config
from agent_runtime.procfs import GroupCensus
from agent_runtime.spawn import KillUntilGoneResult
from agent_runtime_service.cli import EXIT_BAD_CONFIG, main
from agent_runtime_service.cli_version import (
    ProgramVersionRefused,
    claude_version_output,
    codex_version_output,
    require_pinned_versions,
)
from agent_runtime_service.config import ClaudeProgram, CliConfig, CodexProgram, ProgramPin

RUN_TREE_PATTERN = "agent-run-*"
A_LIVE_GROUP = KillUntilGoneResult(GroupCensus.LIVE, ended_in_time=True)
AN_EMPTY_GROUP = KillUntilGoneResult(GroupCensus.EMPTY, ended_in_time=True)
A_SHORT_PROBE_DEADLINE_SECONDS = 0.05
PROVIDER_TOKEN_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "XAI_API_KEY",
    "OPENAI_API_KEY",
)


failure_watch = provider_test_support.failure_watch


def _script(directory: Path, body: str) -> Path:
    program = directory / "claude-script"
    program.write_text(f"#!/bin/sh\n{body}\n")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return program


def _refused_leaving_the_root_empty(
    tmp_path: Path, capsys, program: Path, base: str = VALID_CONFIG
) -> str:
    refusal = refused_on_one_line(config_file_naming(tmp_path, program, base), capsys)
    assert list(working_root_of(tmp_path).iterdir()) == []
    return refusal


def _no_version_line(program: Path, reason: str) -> str:
    return (
        f"cli.anthropic.version: {program} did not print a version ({reason});"
        f" the pinned version is {PINNED_CLAUDE_VERSION}"
    )


@pytest.fixture
def probe_log(tmp_path) -> Path:
    return tmp_path / "probe-starts"


def test_a_program_reporting_the_pinned_version_starts_the_service_without_a_core_runtime(
    tmp_path, working_root, served_runs
):
    reset_config()
    program = _script(tmp_path, f'printf "{PRINTS_THE_PIN}"')

    assert main(["--config", str(config_file_naming(tmp_path, program))]) == 0

    assert served_runs == [{"host": "127.0.0.1", "port": 8099}]
    assert list(working_root.iterdir()) == []


def test_the_probe_runs_in_a_run_tree_below_the_working_root_with_no_token_and_no_login(
    tmp_path, working_root, served_runs, monkeypatch
):
    reset_config()
    parent_home = tmp_path / "parent-home"
    (parent_home / ".claude").mkdir(parents=True)
    (parent_home / ".claude" / ".credentials.json").write_text("login-marker")
    monkeypatch.setenv("HOME", str(parent_home))
    for variable in PROVIDER_TOKEN_VARIABLES:
        monkeypatch.setenv(variable, "token-marker")
    inspecting = tmp_path / "inspection"
    inspecting.mkdir()
    program = _script(
        tmp_path,
        f'env > "{inspecting}/environment"; pwd -P > "{inspecting}/cwd";'
        f' ls -A "$HOME" > "{inspecting}/home-listing";'
        f' ls -A . > "{inspecting}/work-listing"; echo "$HOME" > "{inspecting}/home";'
        f' printf "{PRINTS_THE_PIN}"',
    )

    assert main(["--config", str(config_file_naming(tmp_path, program))]) == 0

    home = Path((inspecting / "home").read_text().strip())
    working_directory = Path((inspecting / "cwd").read_text().strip())
    assert home.parent.parent == working_root.resolve()
    assert fnmatch.fnmatch(home.parent.name, RUN_TREE_PATTERN)
    assert (home.name, working_directory) == ("home", home.parent / "work")
    assert (inspecting / "home-listing").read_text() == ""
    assert (inspecting / "work-listing").read_text() == "output\n"
    seen = {line.split("=", 1)[0] for line in (inspecting / "environment").read_text().splitlines()}
    assert seen.isdisjoint(PROVIDER_TOKEN_VARIABLES)
    assert "marker" not in (inspecting / "environment").read_text()
    assert list(working_root.iterdir()) == []


def test_a_program_reporting_another_version_refuses_the_start_naming_both(
    tmp_path, capsys, served_runs
):
    program = printing(tmp_path, "2.1.303 (Claude Code)\n")

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == (
        f"cli.anthropic.version: {program} reports 2.1.303, not the pinned {PINNED_CLAUDE_VERSION}"
    )
    assert served_runs == []


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        pytest.param("hello\n", "printed 6 characters", id="another-text"),
        pytest.param("2.1.302\n", "printed 8 characters", id="without-the-product-suffix"),
        pytest.param(
            f"{PRINTS_THE_PIN}banner\n",
            f"printed {len(PRINTS_THE_PIN) + 7} characters",
            id="two-lines",
        ),
        pytest.param("", "printed nothing", id="empty"),
        pytest.param("   \n", "printed 4 characters", id="only-spaces"),
        pytest.param("\nhello\n", "printed 7 characters", id="blank-first-line"),
        pytest.param("x" * 100 + "\n", "printed 101 characters", id="long-line"),
    ],
)
def test_output_that_is_not_the_version_line_refuses_the_start_naming_only_how_much_was_printed(
    tmp_path, capsys, served_runs, stdout, reason
):
    program = printing(tmp_path, stdout)

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, reason)
    assert served_runs == []


PRIVATE_MARKER = "private-marker"


def _plain_root(tmp_path: Path) -> tuple[Path, Path]:
    physical = tmp_path / PRIVATE_MARKER
    return physical, physical


def _root_behind_a_symlinked_parent(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "physical").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "physical")
    return tmp_path / "alias" / PRIVATE_MARKER, tmp_path / "physical" / PRIVATE_MARKER


def _root_behind_a_symlink_named_with_an_invalid_utf8_byte(tmp_path: Path) -> tuple[Path, Path]:
    physical_parent = tmp_path / os.fsdecode(b"phys\xff")
    physical_parent.mkdir()
    (tmp_path / "alias").symlink_to(physical_parent)
    return tmp_path / "alias" / PRIVATE_MARKER, physical_parent / PRIVATE_MARKER


def _root_named_with_a_newline(tmp_path: Path) -> tuple[Path, Path]:
    physical = tmp_path / f"{PRIVATE_MARKER}\nrest"
    return physical, physical


@pytest.mark.parametrize(
    ("root_layout", "program_body"),
    [
        pytest.param(_plain_root, 'echo "$HOME"', id="home"),
        pytest.param(_root_behind_a_symlinked_parent, "pwd -P", id="symlinked-parent"),
        pytest.param(_root_named_with_a_newline, 'echo "$HOME"', id="newline-in-the-root-name"),
        pytest.param(
            _root_behind_a_symlink_named_with_an_invalid_utf8_byte,
            "pwd -P",
            id="symlink-target-with-an-invalid-utf8-byte",
        ),
    ],
)
def test_a_program_printing_where_it_runs_refuses_the_start_without_the_working_root(
    tmp_path, capsys, served_runs, root_layout, program_body
):
    configured, physical = root_layout(tmp_path)
    physical.mkdir()
    physical.chmod(0o700)
    printed_copy = tmp_path / "printed-copy"
    program = _script(tmp_path, f'{program_body} | tee "{printed_copy}"')

    refusal = refused_on_one_line(
        written(tmp_path, document_naming(program, configured)), capsys
    )
    printed_characters = len(printed_copy.read_bytes().decode(errors="replace"))

    assert PRIVATE_MARKER not in refusal
    assert "phys" not in refusal
    assert refusal == _no_version_line(program, f"printed {printed_characters} characters")
    assert list(physical.iterdir()) == []
    assert served_runs == []


def test_a_nonzero_exit_refuses_the_start_even_when_the_output_looks_valid(
    tmp_path, capsys, served_runs
):
    program = _script(tmp_path, f'printf "{PRINTS_THE_PIN}"; exit 3')

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, "exit status 3")
    assert served_runs == []


def test_a_program_that_does_not_exist_refuses_the_start_as_unstarted(
    tmp_path, capsys, served_runs,
):
    program = tmp_path / "absent-claude"

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(
        program, "probe could not start or its start deadline expired"
    )


def test_a_program_that_never_finishes_is_stopped_and_refuses_the_start(
    tmp_path, capsys, served_runs, monkeypatch
):
    monkeypatch.setattr(core_process, "PROBE_TIMEOUT_SECONDS", 1)
    program = _script(tmp_path, f'echo $$ > "{tmp_path}/pid"; exec sleep 60')

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, "probe did not complete")
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_output_beyond_the_runner_limit_refuses_the_start_as_incomplete(
    tmp_path, capsys, served_runs,
):
    program = _script(tmp_path, "head -c 70000 /dev/zero | tr '\\0' x")

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, "probe did not complete")


def test_valid_output_with_stderr_beyond_the_runner_limit_still_refuses_as_incomplete(
    tmp_path, capsys, served_runs
):
    program = _script(
        tmp_path, f"printf '{PRINTS_THE_PIN}'; head -c 70000 /dev/zero | tr '\\0' x >&2"
    )

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, "probe did not complete")
    assert served_runs == []


def test_a_binary_path_holding_a_newline_is_refused_on_one_line_with_the_newline_escaped(
    tmp_path, capsys, served_runs
):
    program = tmp_path / "line\nbreak" / "claude"

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == (
        f"cli.anthropic.version: {str(program).replace(chr(10), '\\n')} did not print a version"
        f" (probe could not start or its start deadline expired);"
        f" the pinned version is {PINNED_CLAUDE_VERSION}"
    )


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(FileNotFoundError(errno.ENOENT, "no temporary directory"), id="missing"),
        pytest.param(PermissionError(errno.EACCES, "not writable"), id="permission-denied"),
        pytest.param(OSError(errno.ENOSPC, "no space left on device"), id="disk-full"),
    ],
)
def test_a_run_tree_that_cannot_be_made_refuses_the_start_as_unstarted(
    tmp_path, capsys, served_runs, monkeypatch, failure
):
    program = printing(tmp_path, PRINTS_THE_PIN)

    def cannot_make_a_directory(*_arguments, **_keywords):
        raise failure

    monkeypatch.setattr(tempfile, "mkdtemp", cannot_make_a_directory)

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(
        program, "probe could not start or its start deadline expired"
    )
    assert served_runs == []


@pytest.mark.parametrize(
    ("stdout", "version"),
    [
        pytest.param("2.1.302 (Claude Code)\n", "2.1.302", id="one-terminal-newline"),
        pytest.param("2.1.302 (Claude Code)", "2.1.302", id="no-newline"),
        pytest.param("0.0.0 (Claude Code)\n", "0.0.0", id="zeros"),
        pytest.param("2.1.302 (Claude Code)\n\n", None, id="two-newlines"),
        pytest.param(" 2.1.302 (Claude Code)\n", None, id="leading-space"),
        pytest.param("2.1.302 (Claude Code) \n", None, id="trailing-space"),
        pytest.param("2.1.302 (Claude Code)\r\n", None, id="carriage-return"),
        pytest.param("2.1.302\n", None, id="no-suffix"),
        pytest.param("v2.1.302 (Claude Code)\n", None, id="v-prefix"),
        pytest.param("2.01.302 (Claude Code)\n", None, id="leading-zero"),
        pytest.param("2.1.302-beta (Claude Code)\n", None, id="prerelease"),
        pytest.param("banner\n2.1.302 (Claude Code)\n", None, id="banner-line"),
        pytest.param("", None, id="empty"),
        pytest.param("2.1.30٢ (Claude Code)\n", None, id="non-ascii-digit"),
    ],
)
def test_claude_reads_only_its_exact_version_line(stdout, version):
    assert claude_version_output(stdout) == version


@pytest.mark.parametrize(
    ("stdout", "version"),
    [
        pytest.param("codex-cli 0.163.0\n", "0.163.0", id="one-terminal-newline"),
        pytest.param("codex-cli 0.163.0", "0.163.0", id="no-newline"),
        pytest.param("codex-cli 0.163.0\n\n", None, id="two-newlines"),
        pytest.param("codex-cli 0.163.0\r\n", None, id="carriage-return"),
        pytest.param("codex-cli 0.163.0 \n", None, id="trailing-space"),
        pytest.param("codex-cli  0.163.0\n", None, id="two-spaces"),
        pytest.param("codex-cli v0.163.0\n", None, id="v-prefix"),
        pytest.param("codex-cli 0.163.0-beta\n", None, id="prerelease"),
        pytest.param("codex-cli 0.163.00\n", None, id="leading-zero"),
        pytest.param("0.163.0\n", None, id="no-prefix"),
        pytest.param("banner\ncodex-cli 0.163.0\n", None, id="banner-line"),
        pytest.param("", None, id="empty"),
        pytest.param("codex-cli 0.163.\u0660\n", None, id="non-ascii-digit"),
    ],
)
def test_codex_reads_only_its_exact_version_line(stdout, version):
    assert codex_version_output(stdout) == version


def _a_pin(
    directory: Path, printed: str, version: str, program_type: type[ProgramPin], login: Path
) -> tuple[ProgramPin, Path]:
    directory.mkdir()
    calls = directory / "calls"
    program = fake_cli_executable(directory, {("--version",): printed}, calls_log=calls)
    pin = program_type(
        binary=str(program), version=version, models=frozenset(), token_file=str(login)
    )
    return pin, calls


def test_with_both_programs_pinned_both_are_probed_and_the_first_miss_refuses_naming_it(
    tmp_path, working_root
):
    claude, claude_calls = _a_pin(
        tmp_path / "claude",
        PRINTS_THE_PIN,
        PINNED_CLAUDE_VERSION,
        ClaudeProgram,
        process_token_file(),
    )
    codex, codex_calls = _a_pin(
        tmp_path / "codex",
        "codex-cli 0.163.1\n",
        "0.163.0",
        CodexProgram,
        a_codex_login_file(tmp_path),
    )
    cli = CliConfig(working_root=str(working_root), anthropic=claude, openai=codex)

    with pytest.raises(ProgramVersionRefused) as refused:
        require_pinned_versions(cli)

    assert str(refused.value) == (
        f"cli.openai.version: {codex.binary} reports 0.163.1, not the pinned 0.163.0"
    )
    assert (claude_calls.exists(), codex_calls.exists()) == (True, True)


def _the_unstarted_refusal(program: Path) -> str:
    return _no_version_line(program, "probe could not start or its start deadline expired")


def _the_mismatch_refusal(program: Path) -> str:
    pinned = PINNED_CLAUDE_VERSION
    return f"cli.anthropic.version: {program} reports 2.1.303, not the pinned {pinned}"


def test_a_run_tree_whose_rollback_fails_still_refuses_the_start_as_unstarted(
    tmp_path, working_root, capsys, served_runs, monkeypatch, caplog
):
    program = printing(tmp_path, PRINTS_THE_PIN)
    config_file = config_file_naming(tmp_path, program)
    fail_the_directory_named(monkeypatch, "home", OSError(errno.EIO, "synthetic mkdir failure"))

    with rmtree_refused(monkeypatch):
        refusal = refused_on_one_line(config_file, capsys)

    the_only_tree(working_root)
    assert refusal == _the_unstarted_refusal(program)
    assert removal_errors(caplog) == [REMOVAL_FAILED]
    assert str(working_root) not in caplog.text
    assert served_runs == []


def test_a_probe_tree_that_cannot_be_removed_is_logged_once_and_the_start_still_serves(
    tmp_path, working_root, served_runs, caplog, monkeypatch
):
    program = printing(tmp_path, PRINTS_THE_PIN)
    config_file = config_file_naming(tmp_path, program)

    with rmtree_refused(monkeypatch):
        exit_status = main(["--config", str(config_file)])

    the_only_tree(working_root)
    assert exit_status == 0
    assert removal_errors(caplog) == [REMOVAL_FAILED]
    assert str(working_root) not in caplog.text
    assert len(served_runs) == 1


@pytest.mark.parametrize(
    ("program_name", "stdout", "expected_refusal"),
    [
        pytest.param("absent-claude", None, _the_unstarted_refusal, id="unstarted"),
        pytest.param("claude", "2.1.303 (Claude Code)\n", _the_mismatch_refusal, id="mismatch"),
    ],
)
def test_a_probe_tree_that_cannot_be_removed_never_replaces_the_probe_refusal(
    tmp_path, working_root, capsys, served_runs, monkeypatch, program_name, stdout, expected_refusal
):
    program = tmp_path / program_name if stdout is None else printing(tmp_path, stdout)
    config_file = config_file_naming(tmp_path, program)

    with rmtree_refused(monkeypatch):
        printed = refused_on_one_line(config_file, capsys)

    the_only_tree(working_root)
    assert printed == expected_refusal(program)
    assert served_runs == []


def test_a_probe_tree_stays_below_the_working_root_until_its_group_is_confirmed_gone(
    tmp_path, working_root, served_runs, monkeypatch, failure_watch: RunnerFailureWatch
):
    hold = Hold()
    group_answers = (A_LIVE_GROUP, AN_EMPTY_GROUP)
    child = fake_cli_process(PRINTS_THE_PIN.encode(), gone=group_answers, hold=hold)
    install_cli_door(monkeypatch, lambda _child, *, stderr: child)
    program = tmp_path / "claude"
    config_file = config_file_naming(tmp_path, program)

    exit_status = main(["--config", str(config_file)])
    trees_at_the_return = list(working_root.glob(RUN_TREE_PATTERN))
    hold.proceed.set()
    failure_watch.wait_for_the_runner_threads_to_end()

    assert exit_status == 0
    assert len(trees_at_the_return) == 1
    assert list(working_root.iterdir()) == []
    assert len(served_runs) == 1


def test_a_probe_refused_before_its_spawn_keeps_its_tree_until_the_late_spawn_is_confirmed(
    tmp_path, working_root, capfd, served_runs, monkeypatch, failure_watch: RunnerFailureWatch
):
    program = printing(tmp_path, PRINTS_THE_PIN)
    config_file = config_file_naming(tmp_path, program)
    release_spawn = threading.Event()

    def held_door(child, *, stderr):
        assert release_spawn.wait(timeout=bounds.TEST_WAIT_SECONDS)
        return real_door(child, stderr=stderr)

    real_door = install_cli_door(monkeypatch, held_door)
    monkeypatch.setattr(core_process, "PROBE_TIMEOUT_SECONDS", A_SHORT_PROBE_DEADLINE_SECONDS)

    try:
        exit_status = main(["--config", str(config_file)])
        trees_after_main = list(working_root.glob(RUN_TREE_PATTERN))
    finally:
        release_spawn.set()
    failure_watch.wait_for_the_runner_threads_to_end()

    assert exit_status == EXIT_BAD_CONFIG
    assert capfd.readouterr().err.endswith(f"{_the_unstarted_refusal(program)}\n")
    assert len(trees_after_main) == 1
    assert list(working_root.iterdir()) == []
    assert served_runs == []
