"""The service serves a Codex profile: its runs, their admission and their routing.

Rows serve the real app on a loopback socket and run a stand-in for the pinned CLI as a real
child, with no installed core configuration; ``test_service_codex_startup`` holds the config and
startup rows and takes its builders from here. Test values: one Codex profile ``codex-main``, the
listed model ``gpt-5.5`` and the pinned version 0.163.0.
"""

from __future__ import annotations

import json
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from anchored_support import ChildCleanup
from cli_tools_support import a_tool_app
from codex_support import PRECONDITIONS_MARKER, a_fake_codex
from mcp_app_fake import TestAuthority
from provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    SYSTEM_TEXT,
    a_codex_login,
    a_fake_claude,
    a_private_root,
    held_slots,
)
from pydantic import ValidationError
from service_support import (
    API_GROUP,
    CLI_PROFILE,
    LISTED_MODEL,
    PINNED_CLAUDE_VERSION,
    SONGMAKER_GROUP,
    SONGMAKER_TOOLS,
    SUBSCRIPTION_GROUP,
    TEST_ISSUER,
    _service_config_document,
    _toml_lines,
    assert_rejected,
    decoded_events,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    post_run,
    process_token_file,
    record_signed_tokens,
    run_body,
    run_headers,
)
from test_codex_run import DONE, THREAD, a_message, an_item, line
from test_service_cli_runner import (
    MINT,
    THREE_MESSAGE_TURN,
    HeldReap,
    all_children_gone,
    drive_over_http,
    headers,
    held_reap,  # noqa: F401 - a fixture, requested by name
    leave_after,
    post_a_run,
    private_root_of,
    shown,
    status_of,
)

import agent_runtime_service.cli_runner as cli_runner
from agent_runtime.cli_run import RelayAccess
from agent_runtime.codex.run import CODEX_CLI_PROCESS_POOL
from agent_runtime.config import reset_config
from agent_runtime_client.wire import (
    FailureCode,
    RejectionCode,
    Turn,
)
from agent_runtime_service.capability import Capability
from agent_runtime_service.cli_runner import CliRunner, mcp_tls_context
from agent_runtime_service.config import Provider, ServiceConfig
from agent_runtime_service.run_events import RUNNER_FAILED_MESSAGE
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.runners import AdmittedRun, Runners

CODEX_PROFILE = "codex-main"
CODEX_MODEL = "gpt-5.5"
CODEX_PIN = "0.163.0"
VERSION_LINE = f"codex-cli {CODEX_PIN}"
TURN_STDIN = "User: a\n\nAssistant: b\n\nUser: c"
CANARY = "cli-text-canary-3b71"
PARKED = "exec sleep 600"
ANSWER = (THREAD, a_message("done"), DONE)
LOGIN_NAME = "codex-login.json"


@pytest.fixture(autouse=True)
def no_installed_core_runtime() -> None:
    reset_config()


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, logging_state_restored: None) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: runs.append(where))
    return runs


def write_login(path: Path, contents: bytes, mode: int = 0o600) -> Path:
    """Write the login beside the file and move it over the file, as ``mv -T`` does."""
    beside = path.with_name(path.name + ".new")
    beside.write_bytes(contents)
    beside.chmod(mode)
    beside.replace(path)
    return path


def a_login_file(directory: Path, contents: bytes | None = None) -> Path:
    return write_login(directory / LOGIN_NAME, a_codex_login() if contents is None else contents)


def a_service_fake(directory: Path, stream: tuple[bytes, ...] = ANSWER, then: str = "exit 0", **kw):
    return a_fake_codex(
        directory,
        stream,
        then,
        system_text=SYSTEM_TEXT,
        expected_stdin=TURN_STDIN,
        version_line=VERSION_LINE,
        **kw,
    )


def codex_document(
    tmp_path: Path,
    binary: Path,
    login_file: Path,
    *,
    runs: dict[str, object] | None = None,
    songmaker: dict[str, object] | None = None,
) -> dict[str, Any]:
    document = _service_config_document(TEST_ISSUER, runs, songmaker)
    if songmaker is None:
        del document["apps"]["songmaker"]["mcp_origin"]
    document["defaults"] = {"model": f"{Provider.CODEX}/{CODEX_MODEL}"}
    document["cli"] = {
        "working_root": str(a_private_root(tmp_path)),
        "codex": {"binary": str(binary), "version": CODEX_PIN, "models": [CODEX_MODEL]},
    }
    document["profiles"] = {
        CODEX_PROFILE: {"provider": "codex", "token_file": str(login_file)}
    }
    return document


def with_claude_beside(
    document: dict[str, Any], claude: Path, *, codex_is_default: bool = True
) -> dict[str, Any]:
    document["cli"]["claude"] = {
        "binary": str(claude),
        "version": PINNED_CLAUDE_VERSION,
        "models": [LISTED_MODEL],
    }
    codex = {**document["profiles"][CODEX_PROFILE], "default": codex_is_default}
    document["profiles"] = {
        CLI_PROFILE: {
            "provider": "claude",
            "token_file": str(process_token_file()),
            "default": not codex_is_default,
        },
        CODEX_PROFILE: codex,
    }
    return document


def a_document_with_both(
    tmp_path: Path, stream: tuple[bytes, ...] = ANSWER, *, codex_is_default: bool = True
) -> dict[str, Any]:
    """Claude and Codex profiles, each with a stand-in program of its own under ``tmp_path``."""
    claude_directory = tmp_path / "claude-directory"
    claude_directory.mkdir()
    codex = codex_document(tmp_path, a_service_fake(tmp_path, stream), a_login_file(tmp_path))
    return with_claude_beside(
        codex, a_fake_claude(claude_directory), codex_is_default=codex_is_default
    )


def a_config(document: dict[str, Any]) -> ServiceConfig:
    return ServiceConfig.model_validate(document)


def written(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "agent-runtime-service.toml"
    path.write_text("\n".join(_toml_lines(document)) + "\n", encoding="utf-8")
    return path


def the_refusal_of(document: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as refused:
        a_config(document)
    return str(refused.value)


# Runs through ``POST /runs`` with the stand-in CLI.


def a_codex_service(tmp_path: Path, **options: Any) -> tuple[ServiceConfig, Path]:
    login = a_login_file(tmp_path)
    capture = tmp_path / "capture"
    binary = a_service_fake(tmp_path, capture=capture, **options.pop("fake", {}))
    return a_config(codex_document(tmp_path, binary, login, **options)), capture


def argv_of(capture: Path) -> list[str]:
    return (capture / "argv").read_text().splitlines()


def test_a_codex_run_streams_its_answer_with_the_configured_model_as_one_argument(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, capture = a_codex_service(tmp_path, runs={"max_parallel_cli_runs": 1})

    def scenario(base_url: str) -> list[Any]:
        return [post_a_run(base_url), post_a_run(base_url)]

    first, second = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert [shown(events) for events in (first, second)] == [
        [("assistant_text", "done"), ("succeeded", "done")]
    ] * 2
    assert first[0].type == "run_started"
    assert [a for a in argv_of(capture) if a.startswith("--model")] == [f"--model={CODEX_MODEL}"]
    assert (list(private_root_of(config).iterdir()), held_slots(CODEX_CLI_PROCESS_POOL)) == ([], 0)
    assert all_children_gone(child_cleanup) == [True, True]


def test_a_login_file_swapped_between_two_runs_serves_the_next_run_without_a_restart(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, capture = a_codex_service(tmp_path)
    login = tmp_path / LOGIN_NAME

    def scenario(base_url: str) -> list[str]:
        post_a_run(base_url)
        before = (capture / "auth.json").read_text()
        write_login(login, a_codex_login(tokens={"access_token": "second-access-token"}))
        post_a_run(base_url)
        return [before, (capture / "auth.json").read_text()]

    before, after = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (CANARY_ACCESS_TOKEN in before, "second-access-token" in after) == (True, True)


def an_api_key_document(login: Path) -> None:
    write_login(login, json.dumps({"OPENAI_API_KEY": CANARY_ACCESS_TOKEN}).encode())


def not_json(login: Path) -> None:
    write_login(login, b"not-json " + CANARY_ACCESS_TOKEN.encode())


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(lambda login: login.unlink(), id="missing"),
        pytest.param(lambda login: login.chmod(0o644), id="mode-0644"),
        pytest.param(an_api_key_document, id="api-key-document"),
        pytest.param(not_json, id="not-json"),
    ],
)
def test_a_login_file_gone_bad_fails_its_run_before_the_program_and_a_repair_serves_the_next(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    spoil: Callable[[Path], None],
) -> None:
    config, capture = a_codex_service(tmp_path, runs={"max_parallel_cli_runs": 1})
    login = tmp_path / LOGIN_NAME
    spoil(login)

    def scenario(base_url: str) -> tuple[Any, ...]:
        failed = post_a_run(base_url)
        left_behind = (
            status_of(base_url, failed).failure_code,
            (tmp_path / PRECONDITIONS_MARKER).exists(),
            list(private_root_of(config).iterdir()),
        )
        write_login(login, a_codex_login())
        return failed, left_behind, post_a_run(base_url)

    failed, left_behind, repaired = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(failed), left_behind, shown(repaired)[-1]) == (
        [("failed", RUNNER_FAILED_MESSAGE)],
        (FailureCode.PROVIDER_ERROR, False, []),
        ("succeeded", "done"),
    )
    assert [(event, kind) for event, kind, _ in failure_lines(caplog)] == [
        (f"The runner of run {failed[0].run_id} failed", "UnsoundSecretFile")
    ]
    leaks = (LOGIN_NAME, str(tmp_path), CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN)
    assert [leak for leak in leaks if leak in caplog.text] == []


@pytest.mark.parametrize(
    ("stream", "then", "failure_class"),
    [
        pytest.param(
            (line(type="turn.failed", error={"message": f"401 {CANARY}"}),),
            "exit 1",
            "CodexTurnFailed",
            id="failed-turn-with-a-bare-401",
        ),
        pytest.param(
            (
                line(
                    type="turn.failed",
                    error={"message": f"unexpected status 401 Unauthorized: {CANARY}"},
                ),
            ),
            "exit 1",
            "CliLoginRefusedError",
            id="login-refused",
        ),
        pytest.param(
            (an_item("item.started", "command_execution", command=CANARY),),
            "exit 0",
            "CodexToolItem",
            id="tool-item",
        ),
    ],
)
def test_a_failing_codex_run_ends_with_the_services_text_and_its_closed_cause_only(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stream: tuple[bytes, ...],
    then: str,
    failure_class: str,
) -> None:
    config, _capture = a_codex_service(tmp_path, fake={"stream": stream, "then": then})

    def scenario(base_url: str) -> Any:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(events), status.failure_code) == (
        [("failed", RUNNER_FAILED_MESSAGE)],
        FailureCode.PROVIDER_ERROR,
    )
    assert [kind for _event, kind, _function in failure_lines(caplog)] == [failure_class]
    assert CANARY not in f"{[e.model_dump_json() for e in events]} {caplog.text}"


def test_neither_the_program_arguments_nor_the_service_log_hold_a_login_value(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    config, capture = a_codex_service(tmp_path)

    drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    seen = f"{argv_of(capture)} {caplog.text}"
    assert [c for c in (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN) if c in seen] == []


def test_a_second_codex_run_beyond_the_limit_is_busy_and_never_goes_to_the_api_route(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,  # noqa: F811
) -> None:
    config, _capture = a_codex_service(
        tmp_path,
        runs={"max_parallel_cli_runs": 1},
        fake={"stream": (THREAD, a_message("one")), "then": PARKED},
    )
    both = run_headers(MINT.workload(), MINT.user((SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP)))

    def scenario(base_url: str) -> tuple[int, str]:
        leave_after(base_url, "assistant_text")
        held_reap.wait_until_reached()
        answered = post_run(base_url, both, run_body(turn=THREE_MESSAGE_TURN))
        held_reap.let_go.set()
        return answered.status_code, answered.headers.get("retry-after", "")

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == (429, "10")


# Tools are refused at admission until the relay serves Codex.


def test_a_tool_request_on_a_codex_profile_is_refused_before_any_place_capability_or_child(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,  # noqa: F811
) -> None:
    signed = record_signed_tokens(monkeypatch)
    both = run_headers(MINT.workload(), MINT.user((SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP)))
    with a_tool_app(authority) as app:
        config = a_config(
            codex_document(
                tmp_path,
                a_service_fake(tmp_path, (THREAD, a_message("one")), PARKED),
                a_login_file(tmp_path),
                runs={"max_parallel_cli_runs": 1},
                songmaker={"mcp_origin": app.origin},
            )
        )
        runners = Runners(
            cli=CliRunner(config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock())
        )

        def scenario(base_url: str) -> Any:
            leave_after(base_url, "assistant_text")
            held_reap.wait_until_reached()
            signed_before = len(signed)
            refused = post_run(
                base_url, both, run_body(turn=THREE_MESSAGE_TURN, tools=SONGMAKER_TOOLS)
            )
            held_reap.let_go.set()
            return refused, len(signed) - signed_before

        refused, newly_signed = drive_over_http(
            child_cleanup, monkeypatch, config, scenario, runners
        )

        assert_rejected(refused, RejectionCode.NOT_ENABLED, 403)
        assert (newly_signed, app.fake.accepted_connections) == (0, 0)


def test_the_same_codex_request_without_tools_runs_even_for_an_app_with_a_tool_origin(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with a_tool_app(authority) as app:
        config = a_config(
            codex_document(
                tmp_path,
                a_service_fake(tmp_path, (THREAD, a_message("one"), DONE)),
                a_login_file(tmp_path),
                songmaker={"mcp_origin": app.origin},
            )
        )
        runners = Runners(
            cli=CliRunner(config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock())
        )

        def scenario(base_url: str) -> Any:
            body = run_body(turn=THREE_MESSAGE_TURN, tools={})
            return shown(decoded_events(post_run(base_url, headers(), body).content))[-1]

        assert drive_over_http(child_cleanup, monkeypatch, config, scenario, runners) == (
            "succeeded",
            "one",
        )


def test_a_codex_run_given_a_relay_is_refused_by_an_exception_not_an_assert(
    tmp_path: Path,
) -> None:
    launch = cli_runner.ProfileLaunch(
        Provider.CODEX,
        a_service_fake(tmp_path),
        a_login_file(tmp_path),
        max_tool_calls=1,
        tool_timeout_seconds=1.0,
    )
    inputs = cli_runner.RunInputs(
        Turn.model_validate(THREE_MESSAGE_TURN),
        None,
        RelayAccess(url="https://relay.test", secret="secret"),
    )

    build = cli_runner.codex_run(launch)
    with pytest.raises(ValueError, match="no tool relay"):
        build(inputs)


def test_no_run_adapter_factory_sees_the_admitted_run_or_its_capability() -> None:
    annotations = [
        hint
        for factory in (*cli_runner.RUN_ADAPTERS.values(), cli_runner.RunInputs)
        for hint in typing.get_type_hints(factory).values()
    ]

    assert [t for t in annotations if t in (AdmittedRun, Capability)] == []


@pytest.mark.parametrize(
    ("app_model", "answer"),
    [
        pytest.param(None, "from-codex", id="the-default-model"),
        pytest.param(f"claude/{LISTED_MODEL}", "done", id="the-apps-model"),
    ],
)
def test_a_run_goes_to_the_profile_of_the_configured_models_provider(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    app_model: str | None,
    answer: str,
) -> None:
    document = a_document_with_both(tmp_path, (THREAD, a_message("from-codex"), DONE))
    if app_model is not None:
        document["apps"]["songmaker"]["model"] = app_model

    events = drive_over_http(child_cleanup, monkeypatch, a_config(document), post_a_run)

    assert shown(events)[-1] == ("succeeded", answer)
