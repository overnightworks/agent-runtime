"""The service serves a provider table: its runs, their admission and their routing.

Rows serve the real app on a loopback socket and run a stand-in for the pinned CLI as a real
child, with no installed core configuration, once per table in ``TABLES``; the builders and the
table descriptions are those of ``tests.support.service_table_support``.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.support.anchored_support import ChildCleanup
from tests.support.cli_tools_support import a_tool_app
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.mcp_app_fake import TestAuthority
from tests.support.provider_test_support import a_fake_claude, held_slots
from tests.support.service_cli_runner_support import (
    MINT,
    THREE_MESSAGE_TURN,
    HeldReap,
    a_held_reap,  # noqa: F401 - the fixture is named in its decorator
    all_children_gone,
    drive_over_http,
    headers,
    leave_after,
    post_a_run,
    private_root_of,
    shown,
    status_of,
)
from tests.support.service_support import (
    API_GROUP,
    COWRITER,
    LISTED_MODEL,
    PRESENTATOR_CLIENT,
    SONGMAKER_GROUP,
    SONGMAKER_TOOLS,
    SUBSCRIPTION_GROUP,
    assert_rejected,
    decoded_events,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    post_run,
    record_signed_tokens,
    run_body,
    run_headers,
)
from tests.support.service_table_support import (
    CANARY,
    CODEX_TABLE,
    GROK_TABLE,
    PARKED,
    TABLES,
    FailingRun,
    ProviderTable,
    a_codex_config,
    a_table_service,
    argv_of,
    the_served_runs,  # noqa: F401 - the fixture is named in its decorator
    the_table,  # noqa: F401 - the fixture is named in its decorator
    with_claude_beside,
    write_login,
    written_document,
)

import agent_runtime_service.cli_runner as cli_runner
from agent_runtime.cli_run import RelayAccess
from agent_runtime.grok.run import GrokRun
from agent_runtime.grok_login import assess_grok_login
from agent_runtime_client.wire import FailureCode, RejectionCode, Turn, TurnMessage
from agent_runtime_service.cli import main
from agent_runtime_service.cli_runner import CliRunner, mcp_tls_context
from agent_runtime_service.config import Provider
from agent_runtime_service.run_events import RUNNER_FAILED_MESSAGE
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.runners import Runners

# Runs through ``POST /runs`` with the stand-in CLI.


def test_a_run_streams_its_answer_with_the_configured_model_as_one_argument(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    table: ProviderTable,
) -> None:
    config, capture = a_table_service(table, tmp_path, runs={"max_parallel_cli_runs": 1})

    def scenario(base_url: str) -> list[Any]:
        return [post_a_run(base_url), post_a_run(base_url)]

    first, second = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert [shown(events) for events in (first, second)] == [
        [("assistant_text", "done"), ("succeeded", "done")]
    ] * 2
    assert first[0].type == "run_started"
    assert [a for a in argv_of(capture) if a.startswith("--model")] == [f"--model={table.model}"]
    assert (list(private_root_of(config).iterdir()), held_slots(table.pool)) == ([], 0)
    assert all_children_gone(child_cleanup) == [True, True]


def test_the_installed_login_is_unchanged_by_startup_the_check_and_two_runs(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    served_runs: list[dict[str, Any]],
    table: ProviderTable,
) -> None:
    login = table.login_file(tmp_path)
    document = table.document(tmp_path, table.service_fake(tmp_path), login)
    path = written_document(tmp_path, document)

    def installed() -> tuple[bytes, int, int]:
        status = login.stat()
        return login.read_bytes(), status.st_ino, stat.S_IMODE(status.st_mode)

    before = installed()
    started = [main(["--config", str(path)]), main(["--config", str(path), "--check"])]
    drive_over_http(
        child_cleanup,
        monkeypatch,
        a_codex_config(document),
        lambda base_url: [post_a_run(base_url), post_a_run(base_url)],
    )

    assert (started, len(served_runs)) == ([0, 0], 1)
    assert installed() == before
    assert table.refresh_canary.encode() in before[0]


def test_a_login_file_swapped_between_two_runs_serves_the_next_run_without_a_restart(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    table: ProviderTable,
) -> None:
    config, capture = a_table_service(table, tmp_path)
    login = tmp_path / table.login_name

    def scenario(base_url: str) -> list[str]:
        post_a_run(base_url)
        before = (capture / "auth.json").read_text()
        write_login(login, table.login_document("second-access-token"))
        post_a_run(base_url)
        return [before, (capture / "auth.json").read_text()]

    before, after = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (table.access_canary in before, "second-access-token" in after) == (True, True)


def test_a_grok_run_gets_no_settings_file_and_the_base_arguments_whatever_the_other_providers_set(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    document = GROK_TABLE.document(
        tmp_path,
        GROK_TABLE.service_fake(tmp_path, capture=capture),
        GROK_TABLE.login_file(tmp_path),
    )
    codex_settings = {"openai": {"web_search": "live"}}
    document["defaults"]["cli_settings"] = codex_settings
    document["apps"]["songmaker"]["cli_settings"] = codex_settings
    document["apps"]["songmaker"]["agents"] = {COWRITER: {"cli_settings": codex_settings}}
    config = a_codex_config(document)

    drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    base = GrokRun(
        Path("/grok"),
        assess_grok_login(GROK_TABLE.login_document(None)),
        "",
        (TurnMessage(role="user", text="a"),),
        model=GROK_TABLE.model,
    )
    home_listing = (capture / "home").read_text().split()
    assert "config.toml" not in home_listing
    base_arguments = base.start(Path("/home"), Path("/work")).child.arguments
    assert tuple(argv_of(capture)[2:]) == base_arguments[2:]


def an_api_key_document(table: ProviderTable, login: Path) -> None:
    write_login(login, table.api_key_login)


def not_json(table: ProviderTable, login: Path) -> None:
    write_login(login, b"not-json " + table.access_canary.encode())


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(lambda table, login: login.unlink(), id="missing"),
        pytest.param(lambda table, login: login.chmod(0o644), id="mode-0644"),
        pytest.param(an_api_key_document, id="api-key-document"),
        pytest.param(not_json, id="not-json"),
    ],
)
def test_a_login_file_gone_bad_fails_its_run_before_the_program_and_a_repair_serves_the_next(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    table: ProviderTable,
    spoil: Callable[[ProviderTable, Path], None],
) -> None:
    config, capture = a_table_service(table, tmp_path, runs={"max_parallel_cli_runs": 1})
    login = tmp_path / table.login_name
    spoil(table, login)

    def scenario(base_url: str) -> tuple[Any, ...]:
        failed = post_a_run(base_url)
        left_behind = (
            status_of(base_url, failed).failure_code,
            (tmp_path / table.preconditions_marker).exists(),
            list(private_root_of(config).iterdir()),
        )
        write_login(login, table.login_document(None))
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
    leaks = (table.login_name, str(tmp_path), *table.canaries)
    assert [leak for leak in leaks if leak in caplog.text] == []


@pytest.mark.parametrize(
    ("table", "failing"),
    [
        pytest.param(table, failing, id=f"{table.provider}-{failing.case}")
        for table in TABLES
        for failing in table.failing_runs
    ],
)
def test_a_failing_run_ends_with_the_services_text_and_its_closed_cause_only(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    table: ProviderTable,
    failing: FailingRun,
) -> None:
    config, _capture = a_table_service(
        table, tmp_path, fake={"stream": failing.stream, "then": failing.then}
    )

    def scenario(base_url: str) -> Any:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (shown(events), status.failure_code) == (
        [("failed", RUNNER_FAILED_MESSAGE)],
        FailureCode.PROVIDER_ERROR,
    )
    assert [kind for _event, kind, _function in failure_lines(caplog)] == [failing.failure_class]
    assert CANARY not in f"{[e.model_dump_json() for e in events]} {caplog.text}"
    assert (list(private_root_of(config).iterdir()), held_slots(table.pool)) == ([], 0)


def test_neither_the_program_arguments_nor_the_service_log_hold_a_login_value(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    table: ProviderTable,
) -> None:
    caplog.set_level("DEBUG")
    config, capture = a_table_service(table, tmp_path)

    drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    seen = f"{argv_of(capture)} {caplog.text}"
    assert [c for c in table.canaries if c in seen] == []


def test_a_second_run_beyond_the_limit_is_busy_and_never_goes_to_the_api_route(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,
    table: ProviderTable,
) -> None:
    config, _capture = a_table_service(
        table,
        tmp_path,
        runs={"max_parallel_cli_runs": 1},
        fake={"stream": table.answering("one", False), "then": PARKED},
    )
    both = run_headers(MINT.workload(), MINT.user((SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP)))

    def scenario(base_url: str) -> tuple[int, str]:
        leave_after(base_url, "assistant_text")
        held_reap.wait_until_reached()
        answered = post_run(base_url, both, run_body(turn=THREE_MESSAGE_TURN))
        held_reap.let_go.set()
        return answered.status_code, answered.headers.get("retry-after", "")

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == (429, "10")


# Tools are refused at admission until the relay serves the table.


def test_a_tool_request_on_the_table_is_refused_before_any_place_capability_or_child(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,
    table: ProviderTable,
) -> None:
    signed = record_signed_tokens(monkeypatch)
    both = run_headers(MINT.workload(), MINT.user((SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP)))
    with a_tool_app(authority) as app:
        config = a_codex_config(
            table.document(
                tmp_path,
                table.service_fake(tmp_path, table.answering("one", False), PARKED),
                table.login_file(tmp_path),
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


def test_the_same_request_without_tools_runs_even_for_an_app_with_a_tool_origin(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    table: ProviderTable,
) -> None:
    with a_tool_app(authority) as app:
        config = a_codex_config(
            table.document(
                tmp_path,
                table.service_fake(tmp_path, table.answering("one", True)),
                table.login_file(tmp_path),
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


def test_a_run_given_a_relay_is_refused_by_an_exception_not_an_assert(
    tmp_path: Path, table: ProviderTable
) -> None:
    launch = cli_runner.ProviderLaunch(
        table.provider,
        table.service_fake(tmp_path),
        table.login_file(tmp_path),
        max_tool_calls=1,
        tool_timeout_seconds=1.0,
    )
    inputs = cli_runner.RunInputs(
        Turn.model_validate(THREE_MESSAGE_TURN),
        table.model,
        RelayAccess(url="https://relay.test", secret="secret"),
        (),
    )

    build = cli_runner.RUN_ADAPTERS[table.provider](launch)
    with pytest.raises(ValueError, match="no tool relay"):
        build(inputs)


ROTATIONS = [
    pytest.param((first, second, third), id="-".join((first, second, third)))
    for first, second, third in (
        (Provider.ANTHROPIC, Provider.OPENAI, Provider.XAI),
        (Provider.OPENAI, Provider.XAI, Provider.ANTHROPIC),
        (Provider.XAI, Provider.ANTHROPIC, Provider.OPENAI),
    )
]


@pytest.mark.parametrize("providers_by_level", ROTATIONS)
def test_a_run_goes_to_the_program_of_the_provider_its_defaults_app_or_agent_model_names(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    providers_by_level: tuple[Provider, Provider, Provider],
) -> None:
    directories = {name: tmp_path / name for name in ("openai", "xai", "anthropic")}
    for directory in directories.values():
        directory.mkdir()
    document, grok_document = (
        table.document(
            directories[table.provider],
            table.service_fake(
                directories[table.provider], table.answering(table.provider.value, True)
            ),
            table.login_file(directories[table.provider]),
        )
        for table in (CODEX_TABLE, GROK_TABLE)
    )
    document["cli"][Provider.XAI] = grok_document["cli"][Provider.XAI]
    with_claude_beside(document, a_fake_claude(directories["anthropic"]))
    models = {
        Provider.ANTHROPIC: f"{Provider.ANTHROPIC}/{LISTED_MODEL}",
        Provider.OPENAI: CODEX_TABLE.listed,
        Provider.XAI: GROK_TABLE.listed,
    }
    by_defaults, by_app, by_agent = providers_by_level
    document["defaults"] = {"model": models[by_defaults]}
    document["apps"]["songmaker"]["model"] = models[by_app]
    document["apps"]["songmaker"]["agents"] = {COWRITER: {"model": models[by_agent]}}
    presentator = run_headers(
        MINT.workload(client=PRESENTATOR_CLIENT),
        MINT.user(azp=PRESENTATOR_CLIENT, groups=[SUBSCRIPTION_GROUP, "/apps/presentator"]),
    )

    def scenario(base_url: str) -> list[tuple[str, str]]:
        runs = [
            (presentator, COWRITER),
            (headers(), "an-agent-without-a-model"),
            (headers(), COWRITER),
        ]
        return [
            shown(
                decoded_events(
                    post_run(
                        base_url, who, run_body(turn=THREE_MESSAGE_TURN, agent=agent)
                    ).content
                )
            )[-1]
            for who, agent in runs
        ]

    answered = drive_over_http(child_cleanup, monkeypatch, a_codex_config(document), scenario)

    answers = {Provider.ANTHROPIC: "done", Provider.OPENAI: "openai", Provider.XAI: "xai"}
    assert answered == [("succeeded", answers[provider]) for provider in providers_by_level]
