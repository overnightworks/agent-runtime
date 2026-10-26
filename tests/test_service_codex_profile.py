"""The service serves a Codex profile: its config, its startup checks and its runs.

Config rows validate documents (and ``cli.main`` for the refusal a person reads); run rows serve
the real app on a loopback socket and run a stand-in for the pinned CLI as a real child, with no
installed core configuration. Test values: one Codex profile ``codex-main``, the listed model
``gpt-5.5`` and the pinned version 0.163.0.
"""

from __future__ import annotations

import json
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
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
    everything_a_refusal_holds,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    post_run,
    process_token_file,
    reaches_text,
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
from test_service_config import _refused_on_one_line
from test_service_ticket_runs import minted_ticket
from ticket_run_support import running_service, ticket_headers

import agent_runtime_service.cli_runner as cli_runner
from agent_runtime.cli_run import RelayAccess
from agent_runtime.codex.run import CODEX_CLI_PROCESS_POOL
from agent_runtime.config import reset_config
from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_client.wire import (
    FailureCode,
    Offer,
    OfferRoute,
    RejectionCode,
    Turn,
    parse_offer,
)
from agent_runtime_service.capability import Capability
from agent_runtime_service.cli import main
from agent_runtime_service.cli_runner import CliRunner, mcp_tls_context
from agent_runtime_service.config import Provider, ServiceConfig
from agent_runtime_service.routing import Api, Cli
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
    document["cli"] = {
        "working_root": str(a_private_root(tmp_path)),
        "codex": {"binary": str(binary), "version": CODEX_PIN, "models": [CODEX_MODEL]},
    }
    document["profiles"] = {
        CODEX_PROFILE: {"provider": "codex", "token_file": str(login_file), "background": True}
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
            "background": False,
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


# Startup: lines 3, 7, 14 and the matrix cells S1 to S10.


def test_a_codex_profile_starts_the_service_after_its_program_proves_its_pin(
    tmp_path: Path, served: list[dict[str, Any]]
) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path))

    assert main(["--config", str(written(tmp_path, document))]) == 0
    assert len(served) == 1


def test_a_program_reporting_another_version_stops_the_start_naming_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = a_service_fake(tmp_path)
    document = codex_document(tmp_path, binary, a_login_file(tmp_path))
    document["cli"]["codex"]["version"] = "0.164.0"

    refusal = _refused_on_one_line(written(tmp_path, document), capsys)

    assert refusal == f"cli.codex.version: {binary} reports {CODEX_PIN}, not the pinned 0.164.0"


@pytest.mark.parametrize(
    ("without", "refusal"),
    [
        pytest.param(
            "token_file",
            f"profiles.{CODEX_PROFILE}.token_file: required for a codex profile",
            id="token-file",
        ),
        pytest.param("cli.codex", "cli.codex: required for a codex profile", id="program-table"),
    ],
)
def test_a_codex_profile_without_its_login_or_program_table_is_refused_before_any_read(
    tmp_path: Path, without: str, refusal: str
) -> None:
    open_login = a_login_file(tmp_path)
    open_login.chmod(0o644)
    document = codex_document(tmp_path, a_service_fake(tmp_path), open_login)
    if without == "token_file":
        del document["profiles"][CODEX_PROFILE]["token_file"]
    else:
        del document["cli"]["codex"]

    shown_refusal = the_refusal_of(document)
    assert refusal in shown_refusal
    assert "open to group" not in shown_refusal


def a_directory_instead(directory: Path) -> Path:
    (directory / LOGIN_NAME).mkdir()
    return directory / LOGIN_NAME


def a_login_that_is(contents: bytes, mode: int = 0o600) -> Callable[[Path], Path]:
    return lambda directory: write_login(directory / LOGIN_NAME, contents, mode)


NOT_A_DOCUMENT = "is not a Codex login document"
NO_TOKENS = (
    "is not a complete Codex sign-in; an API key (OPENAI_API_KEY) is not a subscription login"
)
LOGIN_FAULTS = [
    pytest.param(
        lambda directory: directory / LOGIN_NAME, "cannot be read: No such file", id="missing"
    ),
    pytest.param(
        a_login_that_is(a_codex_login(), 0o644), "is open to group or others", id="mode-0644"
    ),
    pytest.param(a_directory_instead, "is not a regular file", id="directory"),
    pytest.param(
        a_login_that_is(b" " * (MAX_SECRET_FILE_BYTES + 1)),
        f"is larger than {MAX_SECRET_FILE_BYTES} bytes",
        id="over-16-KiB",
    ),
    pytest.param(
        a_login_that_is(b"sk-api-key-line-" + CANARY_ACCESS_TOKEN.encode()),
        NOT_A_DOCUMENT,
        id="bare-key",
    ),
    pytest.param(
        a_login_that_is(b"sk-ant-oat01-" + CANARY_ACCESS_TOKEN.encode()),
        NOT_A_DOCUMENT,
        id="claude-token",
    ),
    pytest.param(
        a_login_that_is(json.dumps({"OPENAI_API_KEY": CANARY_ACCESS_TOKEN}).encode()),
        NO_TOKENS,
        id="api-key-document",
    ),
    pytest.param(
        a_login_that_is(a_codex_login(tokens={"access_token": 5})),
        NOT_A_DOCUMENT,
        id="malformed-tokens",
    ),
    pytest.param(
        a_login_that_is(a_codex_login(tokens={"id_token": ""})), NO_TOKENS, id="empty-token"
    ),
]


@pytest.mark.parametrize(("make", "reason"), LOGIN_FAULTS)
def test_a_login_file_that_is_not_a_sound_codex_login_stops_the_start_naming_the_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], make: Callable[[Path], Path], reason: str
) -> None:
    login = make(tmp_path)
    document = codex_document(tmp_path, a_service_fake(tmp_path), login)

    refusal = _refused_on_one_line(written(tmp_path, document), capsys)

    assert refusal.startswith(f"profiles.{CODEX_PROFILE}.token_file: {login} {reason}")
    assert [c for c in (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN) if c in refusal] == []


@pytest.mark.parametrize(("make", "reason"), LOGIN_FAULTS)
def test_a_refused_login_holds_no_token_in_what_the_refusal_holds(
    tmp_path: Path, make: Callable[[Path], Path], reason: str
) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), make(tmp_path))
    with pytest.raises(ValidationError) as refused:
        a_config(document)

    held = everything_a_refusal_holds(refused.value)
    assert reason in str(refused.value)
    assert not reaches_text(held, [CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN])


def test_a_link_to_a_sound_login_loads_and_a_link_to_an_open_one_is_judged_by_its_target(
    tmp_path: Path,
) -> None:
    sound = a_login_file(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(sound)
    document = codex_document(tmp_path, a_service_fake(tmp_path), link)

    assert a_config(document).profiles[CODEX_PROFILE].token_file == link

    sound.chmod(0o644)
    assert "is open to group or others" in the_refusal_of(document)


def test_a_claude_profile_whose_file_holds_a_codex_document_is_refused_as_json(
    tmp_path: Path,
) -> None:
    document = _service_config_document(TEST_ISSUER, None, None, token_file=a_login_file(tmp_path))

    assert "holds a JSON document" in the_refusal_of(document)


def test_a_grok_profile_is_refused_naming_its_provider(tmp_path: Path) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path))
    document["profiles"] = {"grok-main": {"provider": "grok", "background": False}}

    assert (
        "profiles.grok-main.provider: grok has no subscription route yet;"
        " only claude and codex can have a profile"
    ) in the_refusal_of(document)


def test_a_second_codex_profile_is_refused(tmp_path: Path) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path))
    document["profiles"]["codex-spare"] = dict(document["profiles"][CODEX_PROFILE])

    assert "profiles.codex-spare.provider: codex already has profile 'codex-main'" in (
        the_refusal_of(document)
    )


def test_a_claude_and_a_codex_profile_load_together_and_list_their_programs_in_order(
    tmp_path: Path,
) -> None:
    config = a_config(a_document_with_both(tmp_path))

    assert config.cli is not None
    assert list(config.cli.programs()) == [Provider.CLAUDE, Provider.CODEX]


def test_a_codex_only_installation_lists_only_its_codex_program(tmp_path: Path) -> None:
    config = a_config(codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path)))

    assert config.cli is not None
    assert list(config.cli.programs()) == [Provider.CODEX]


# The default profile (ruled 26.10.2026, line "no model asked").


def test_a_lone_profile_is_the_default_whether_or_not_it_is_marked(tmp_path: Path) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path))
    marked_off = dict(document["profiles"][CODEX_PROFILE], default=False)
    unmarked = a_config(document).profiles[CODEX_PROFILE].default
    document["profiles"][CODEX_PROFILE] = marked_off

    assert (unmarked, a_config(document).profiles[CODEX_PROFILE].default) == (True, True)


@pytest.mark.parametrize(
    ("marks", "refusal"),
    [
        pytest.param(
            (False, False),
            f"profiles.{CLI_PROFILE}.default: one profile must be marked default = true",
            id="none",
        ),
        pytest.param(
            (True, True),
            f"profiles.{CODEX_PROFILE}.default: only one profile may be marked default = true",
            id="two",
        ),
        pytest.param(
            (False, "yes"), f"profiles.{CODEX_PROFILE}.default", id="not-a-boolean-is-named"
        ),
    ],
)
def test_two_profiles_need_exactly_one_default(
    tmp_path: Path, marks: tuple[bool, bool | str], refusal: str
) -> None:
    document = a_document_with_both(tmp_path)
    document["profiles"][CLI_PROFILE]["default"], document["profiles"][CODEX_PROFILE]["default"] = (
        marks
    )

    assert refusal in the_refusal_of(document)


# Runs through ``POST /runs`` with the stand-in CLI.


def a_codex_service(tmp_path: Path, **options: Any) -> tuple[ServiceConfig, Path]:
    login = a_login_file(tmp_path)
    capture = tmp_path / "capture"
    binary = a_service_fake(tmp_path, capture=capture, **options.pop("fake", {}))
    return a_config(codex_document(tmp_path, binary, login, **options)), capture


def argv_of(capture: Path) -> list[str]:
    return (capture / "argv").read_text().splitlines()


@pytest.mark.parametrize(
    ("requested", "model_options"),
    [
        pytest.param(f"codex/{CODEX_MODEL}", [f"--model={CODEX_MODEL}"], id="listed-model"),
        pytest.param(None, [], id="no-model"),
    ],
)
def test_a_codex_run_streams_its_answer_with_the_listed_model_as_one_argument_or_none(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    requested: str | None,
    model_options: list[str],
) -> None:
    config, capture = a_codex_service(tmp_path, runs={"max_parallel_cli_runs": 1})

    def scenario(base_url: str) -> list[Any]:
        return [post_a_run(base_url, model=requested), post_a_run(base_url, model=requested)]

    first, second = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert [shown(events) for events in (first, second)] == [
        [("assistant_text", "done"), ("succeeded", "done")]
    ] * 2
    assert first[0].type == "run_started"
    assert [a for a in argv_of(capture) if a.startswith("--model")] == model_options
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
            id="failed-turn",
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


SUBSCRIBER = (SUBSCRIPTION_GROUP, SONGMAKER_GROUP)


@pytest.mark.parametrize(
    ("codex_is_default", "groups", "route"),
    [
        pytest.param(True, SUBSCRIBER, Cli(CODEX_PROFILE), id="default-is-background"),
        pytest.param(False, (*SUBSCRIBER, API_GROUP), Api("assistant-api"), id="api-fallback"),
        pytest.param(False, SUBSCRIBER, None, id="not-enabled-without-api-rights"),
    ],
)
def test_a_ticket_run_on_two_profiles_takes_only_the_background_one(
    tmp_path: Path, codex_is_default: bool, groups: tuple[str, ...], route: Cli | Api | None
) -> None:
    config = a_config(a_document_with_both(tmp_path, codex_is_default=codex_is_default))
    with running_service(config, MINT.fetch_key_set) as service:
        ticket = minted_ticket(service.base_url, MINT, groups)

        response = post_run(service.base_url, ticket_headers(MINT.workload(), ticket), run_body())

        if route is None:
            assert_rejected(response, RejectionCode.NOT_ENABLED, 403)
        else:
            assert response.status_code == 200
            assert [run.route for run in (*service.admitted(), *service.api.admitted)] == [route]


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

    with pytest.raises(ValueError, match="no tool relay"):
        cli_runner.codex_run(launch)(inputs)


def test_no_run_adapter_factory_sees_the_admitted_run_or_its_capability() -> None:
    annotations = [
        hint
        for factory in (*cli_runner.RUN_ADAPTERS.values(), cli_runner.RunInputs)
        for hint in typing.get_type_hints(factory).values()
    ]

    assert [t for t in annotations if t in (AdmittedRun, Capability)] == []


def test_the_offer_lists_the_codex_model_on_the_subscription_route(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _capture = a_codex_service(tmp_path)

    def scenario(base_url: str) -> bytes:
        return httpx.get(f"{base_url}/offer", headers=headers()).content

    offer = parse_offer(drive_over_http(child_cleanup, monkeypatch, config, scenario))

    assert isinstance(offer, Offer)
    assert [(m.id, m.route, m.ready, m.owned_by) for m in offer.data] == [
        (f"codex/{CODEX_MODEL}", OfferRoute.CLI, True, "codex")
    ]


def test_a_run_with_no_model_goes_to_the_default_profile_and_a_named_model_to_its_own(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_config(a_document_with_both(tmp_path, (THREAD, a_message("from-codex"), DONE)))

    def scenario(base_url: str) -> list[Any]:
        return [
            shown(post_a_run(base_url, model=model))[-1]
            for model in (None, f"claude/{LISTED_MODEL}", f"codex/{CODEX_MODEL}")
        ]

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == [
        ("succeeded", "from-codex"),
        ("succeeded", "done"),
        ("succeeded", "from-codex"),
    ]
