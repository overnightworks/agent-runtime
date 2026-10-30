"""A run may name a model (decision record 0001, the model-per-run ruling, protocol 5).

The configuration owns what is offered (``ServiceConfig.offered_models()``) and the default; a
request names an offered model or none. Every test goes through the HTTP entrance with real
token checks, on the person's path and on the app's own path.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from logging import DEBUG

import httpx
import pytest
from tests.support.api_runner_support import answer, configured, fake_gateway
from tests.support.bounds import TEST_WAIT_SECONDS
from tests.support.codex_support import DEFAULT_SETTINGS
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.held_runner_support import HeldRunner
from tests.support.provider_test_support import a_fake_claude
from tests.support.service_cli_runner_support import (
    THREE_MESSAGE_TURN,
    drive_over_http,
    headers,
    shown,
)
from tests.support.service_support import (
    API_GROUP,
    COWRITER,
    PRESENTATOR_CLIENT,
    SONGMAKER_GROUP,
    SONGMAKER_TOOLS,
    SUBSCRIPTION_GROUP,
    ScriptedRunner,
    TokenMint,
    answer_of,
    assert_rejected,
    decoded_events,
    post_run,
    run_body,
    run_headers,
    served,
    service_config,
)
from tests.support.service_table_support import (
    CODEX_MODEL,
    CODEX_NAME,
    CODEX_TABLE,
    GROK_TABLE,
    a_codex_config,
    a_codex_login_file,
    a_config_with_both_providers,
    a_service_fake,
    argv_of,
    captured_settings,
    codex_document,
    with_claude_beside,
    with_levels,
)
from webauth.https_client import KeySetFetchError

from agent_runtime.config import reset_config
from agent_runtime.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_runtime_client.wire import ModelList, RejectionCode, RunEvent, RunStarted
from agent_runtime_service.app import create_app
from agent_runtime_service.cli import installed_runners
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.runners import Runners


@dataclass(frozen=True)
class _Models:
    sonnet: str = "anthropic/sonnet"
    opus: str = "anthropic/opus"
    haiku: str = "anthropic/haiku"
    not_offered: str = "anthropic/unlisted"
    claude: tuple[str, ...] = ("sonnet", "opus", "haiku")


@pytest.fixture
def models() -> _Models:
    return _Models()


def _config(
    songmaker: Mapping[str, object] | None = None, runs: Mapping[str, object] | None = None
) -> ServiceConfig:
    return service_config(runs=runs, songmaker=songmaker, claude_models=_Models().claude)


@dataclass(frozen=True)
class _Service:
    base_url: str
    cli: ScriptedRunner
    api: ScriptedRunner
    mint: TokenMint

    def person_run(self, groups: list[str] | None = None, **body: object) -> httpx.Response:
        person = self.mint.user(groups=groups or [SUBSCRIPTION_GROUP, SONGMAKER_GROUP])
        return post_run(self.base_url, run_headers(self.mint.workload(), person), run_body(**body))

    def app_run(self, **body: object) -> httpx.Response:
        return post_run(
            self.base_url,
            run_headers(self.mint.workload(groups=[SUBSCRIPTION_GROUP]), None),
            run_body(**body),
        )

    @property
    def started(self) -> int:
        return len(self.cli.admitted) + len(self.api.admitted)

    @property
    def model_run(self) -> str:
        return (self.cli.admitted + self.api.admitted)[-1].model


@contextmanager
def _serving(
    config: ServiceConfig | None = None,
    *,
    cli_script: Sequence[StreamEvent | Exception] = (
        AssistantTextEvent(text="via the CLI"),
        FinalEvent(text="CLI answer"),
    ),
    cli: ScriptedRunner | HeldRunner | None = None,
    fetch_key_set: Callable[[str], bytes] | None = None,
) -> Iterator[_Service]:
    mint = TokenMint()
    api_script = (AssistantTextEvent(text="via the API"), FinalEvent(text="API answer"))
    cli, api = cli or ScriptedRunner(cli_script), ScriptedRunner(api_script)
    app = create_app(
        config or _config(),
        runners=Runners(cli=cli, api=api),
        fetch_key_set=fetch_key_set or mint.fetch_key_set,
    )
    with served(app) as base_url:
        yield _Service(base_url, cli, api, mint)


@pytest.fixture
def service() -> Iterator[_Service]:
    songmaker = {"agents": {COWRITER: {"runs_as_app": True}}}
    with _serving(_config(songmaker)) as serving:
        yield serving


def _started(response: httpx.Response) -> RunStarted:
    assert response.status_code == 200, response.text
    started = decoded_events(response.content)[0]
    assert isinstance(started, RunStarted)
    return started


@pytest.mark.parametrize(
    ("songmaker", "expected"),
    [
        pytest.param(
            {"model": "anthropic/haiku", "agents": {COWRITER: {"model": "anthropic/opus"}}},
            "anthropic/opus",
            id="the-agents",
        ),
        pytest.param({"model": "anthropic/haiku"}, "anthropic/haiku", id="the-apps"),
        pytest.param({}, "anthropic/sonnet", id="the-default"),
    ],
)
@pytest.mark.parametrize("named", [pytest.param(False, id="absent"), pytest.param(True, id="null")])
def test_m1_to_m4_a_run_naming_no_model_runs_the_configured_one(songmaker, expected, named):
    body = {"model": None} if named else {}
    with _serving(_config(songmaker)) as serving:
        started = _started(serving.person_run(**body))

    assert (started.model, serving.model_run) == (expected, expected)


def test_m5_a_named_offered_model_wins_over_the_configured_levels(models):
    config = _config({"model": models.haiku, "agents": {COWRITER: {"model": models.opus}}})
    with _serving(config) as serving:
        started = _started(serving.person_run(model=models.sonnet))

    assert (started.model, serving.model_run) == (models.sonnet, models.sonnet)


def test_m6_a_model_of_another_provider_runs_on_that_providers_route(
    tmp_path, child_cleanup, monkeypatch, models
):
    capture = tmp_path / "capture"
    claude_directory = tmp_path / "claude-directory"
    claude_directory.mkdir()
    codex = codex_document(
        tmp_path, a_service_fake(tmp_path, capture=capture), a_codex_login_file(tmp_path)
    )
    document = with_claude_beside(codex, a_fake_claude(claude_directory))
    document["defaults"] = {"model": models.sonnet}
    document = with_levels(
        document,
        CODEX_TABLE,
        COWRITER,
        {"web_search": "live", "sandbox_mode": "danger-full-access"},
        {"sandbox_mode": "workspace-write"},
        {"approval_policy": "on-request"},
    )

    def run_naming_codex(base_url: str) -> list[RunEvent]:
        body = run_body(turn=THREE_MESSAGE_TURN, model=CODEX_NAME)
        return decoded_events(post_run(base_url, headers(), body).content)

    events = drive_over_http(child_cleanup, monkeypatch, a_codex_config(document), run_naming_codex)

    assert (events[0].model, shown(events)[-1]) == (CODEX_NAME, ("succeeded", "done"))
    assert [a for a in argv_of(capture) if a.startswith("--model")] == [f"--model={CODEX_MODEL}"]
    assert captured_settings(capture) == {
        **DEFAULT_SETTINGS,
        "web_search": "live",
        "sandbox_mode": "workspace-write",
        "approval_policy": "on-request",
    }


@pytest.mark.parametrize("model", ["anthropic/unlisted", "openai/unlisted"])
def test_m7_a_model_that_is_not_offered_is_refused_and_starts_nothing(service, model):
    response = service.person_run(model=model)

    assert_rejected(response, RejectionCode.MODEL_NOT_OFFERED, 422)
    assert service.started == 0


@pytest.mark.parametrize("model", ["Opus", "anthropic", "anthropic/opus@1", "ANTHROPIC/opus", ""])
def test_m8_a_malformed_model_is_an_invalid_request(service, model):
    assert_rejected(service.person_run(model=model), RejectionCode.INVALID_REQUEST, 422)


def test_m9_the_apps_own_run_may_name_an_offered_model(service, models):
    started = _started(service.app_run(model=models.opus))

    assert (started.model, service.model_run) == (models.opus, models.opus)


def test_m10_the_apps_own_run_naming_a_model_that_is_not_offered_is_refused(service, models):
    assert_rejected(service.app_run(model=models.not_offered), RejectionCode.MODEL_NOT_OFFERED, 422)
    assert service.started == 0


def test_m11_a_person_without_the_role_is_forbidden_before_the_model_is_checked(service, models):
    response = service.person_run(model=models.not_offered, roles=["admin"])

    assert_rejected(response, RejectionCode.FORBIDDEN, 403)


def test_m12_tools_without_an_mcp_origin_are_forbidden_before_the_model_is_checked(models):
    config = _config({"mcp_origin": None})
    with _serving(config) as serving:
        response = serving.person_run(model=models.not_offered, tools=SONGMAKER_TOOLS)

    assert_rejected(response, RejectionCode.FORBIDDEN, 403)


def test_m13_an_invalid_user_token_is_unauthorized_whatever_model_is_named(service, models):
    headers = run_headers(service.mint.workload(), "not-a-token")

    response = post_run(service.base_url, headers, run_body(model=models.not_offered))

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)


def test_m14_an_unreachable_key_set_is_unavailable_whatever_model_is_named(models):
    def unreachable_issuer(_url: str) -> bytes:
        raise KeySetFetchError("the issuer cannot be reached")

    with _serving(fetch_key_set=unreachable_issuer) as serving:
        response = serving.person_run(model=models.not_offered)

    assert_rejected(response, RejectionCode.UNAVAILABLE, 503)


def test_m15_a_job_ticket_header_is_unauthorized_before_any_token_is_read(service, models):
    headers = {"Agent-Job-Ticket": "ticket", **run_headers(service.mint.workload(), None)}

    response = post_run(service.base_url, headers, run_body(model=models.not_offered))

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)


def test_m16_an_api_only_caller_names_an_offered_model_and_the_gateway_receives_it(
    tmp_path, monkeypatch, models
):
    received = []

    def gateway(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=answer())

    fake_gateway(monkeypatch, gateway)
    reset_config()
    config = configured(tmp_path, claude_models=models.claude)
    app = create_app(
        config,
        runners=installed_runners(config, None, LoopClock()),
        fetch_key_set=(mint := TokenMint()).fetch_key_set,
    )

    with served(app) as base_url:
        headers = run_headers(mint.workload(), mint.user(groups=[API_GROUP, SONGMAKER_GROUP]))
        events = decoded_events(post_run(base_url, headers, run_body(model=models.opus)).content)

    assert (events[0].model, events[-1].type, received) == (models.opus, "succeeded", [models.opus])


def test_m17_a_caller_in_no_route_group_is_not_enabled_though_the_model_is_offered(service, models):
    response = service.person_run([SONGMAKER_GROUP], model=models.opus)

    assert_rejected(response, RejectionCode.NOT_ENABLED, 403)
    assert service.started == 0


def test_m18_tools_on_a_codex_model_are_admitted(tmp_path):
    with _serving(a_config_with_both_providers(tmp_path)) as serving:
        response = serving.person_run(model=CODEX_NAME, tools=SONGMAKER_TOOLS)

    assert response.status_code == 200, response.text
    assert serving.started == 1


def test_m18_tools_on_a_grok_model_are_not_enabled(tmp_path):
    with _serving(a_config_with_both_providers(tmp_path, GROK_TABLE)) as serving:
        response = serving.person_run(model=GROK_TABLE.listed, tools=SONGMAKER_TOOLS)

    assert_rejected(response, RejectionCode.NOT_ENABLED, 403)
    assert serving.started == 0


def test_m19_a_full_subscription_answers_busy_and_never_falls_back_to_the_api_route(models):
    held = HeldRunner()
    config = _config(runs={"max_parallel_cli_runs": 1})
    both_routes = [SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP]
    with ThreadPoolExecutor() as pool, _serving(config, cli=held) as serving:
        try:
            pending = pool.submit(serving.person_run, both_routes, model=models.opus)
            assert held.entered.wait(TEST_WAIT_SECONDS), "the run was not admitted"

            busy = serving.person_run(both_routes, model=models.haiku)

            assert_rejected(busy, RejectionCode.BUSY, 429)
            assert serving.api.admitted == []
        finally:
            held.proceed_all()
            pending.result(timeout=TEST_WAIT_SECONDS)


@pytest.mark.parametrize("model", [{"model": "anthropic/opus"}, {}])
def test_m20_a_body_of_protocol_4_is_unsupported(service, model):
    response = service.person_run(v=4, **model)

    assert_rejected(response, RejectionCode.UNSUPPORTED_VERSION, 400)


def test_m25_a_model_whose_run_fails_falls_back_to_no_other_model(models):
    with _serving(_config(), cli_script=[RuntimeError("provider error")]) as serving:
        events = decoded_events(serving.person_run(model=models.opus).content)

    assert [(event.type, getattr(event, "model", None)) for event in events][0] == (
        "run_started",
        models.opus,
    )
    assert events[-1].type != "succeeded"
    assert [run.model for run in serving.cli.admitted] == [models.opus]
    assert serving.api.admitted == []


def _get_models(service: _Service, headers: Mapping[str, str] | None = None) -> httpx.Response:
    return httpx.get(f"{service.base_url}/models", headers=dict(headers or {}), timeout=10)


def _workload(service: _Service, **overrides: object) -> dict[str, str]:
    return run_headers(service.mint.workload(**overrides), None)


def test_n1_n8_the_workload_token_alone_of_an_app_with_no_own_agent_reads_the_sorted_offer(
    tmp_path,
):
    config = a_config_with_both_providers(tmp_path)
    with _serving(config) as serving:
        response = _get_models(serving, _workload(serving))

    listed = ModelList.model_validate_json(response.content)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert [entry.id for entry in listed.data] == sorted(config.offered_models())
    assert len(listed.data) > 1
    assert {entry.object for entry in listed.data} == {"model"}


def test_n2_an_invalid_authorization_header_is_ignored():
    with _serving() as serving:
        plain = _get_models(serving, _workload(serving))
        with_user = _get_models(
            serving, {**_workload(serving), "Authorization": "Bearer not.a.token"}
        )

    assert (with_user.status_code, with_user.content) == (200, plain.content)


def test_n3_a_job_ticket_header_is_unauthorized_before_any_token_is_read():
    with _serving() as serving:
        response = _get_models(serving, {"Agent-Job-Ticket": "anything"})
        with_valid_token = _get_models(serving, {**_workload(serving), "Agent-Job-Ticket": "x"})

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)
    assert_rejected(with_valid_token, RejectionCode.UNAUTHORIZED, 401)
    assert serving.mint.key_set_fetches == 0


def test_n4_n5_a_missing_or_unusable_workload_token_is_unauthorized():
    other_key = TokenMint()
    with _serving() as serving:
        now = int(time.time())
        refused = [
            _get_models(serving),
            _get_models(serving, run_headers("not.a.jwt", None)),
            _get_models(serving, run_headers(other_key.workload(), None)),
            _get_models(serving, _workload(serving, iat=now - 400, exp=now - 100)),
        ]

    for response in refused:
        assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)


def test_n6_an_unreachable_key_set_is_unavailable_with_retry_after():
    def unreachable_issuer(_url: str) -> bytes:
        raise KeySetFetchError("the issuer cannot be reached")

    with _serving(fetch_key_set=unreachable_issuer) as serving:
        response = _get_models(serving, _workload(serving))

    assert_rejected(response, RejectionCode.UNAVAILABLE, 503)


def test_n7_a_workload_token_bound_to_no_registered_app_is_forbidden():
    with _serving() as serving:
        response = _get_models(serving, _workload(serving, client="unregistered-client"))

    assert_rejected(response, RejectionCode.FORBIDDEN, 403)


def test_n9_every_registered_app_reads_the_same_bytes():
    with _serving() as serving:
        songmaker = _get_models(serving, _workload(serving))
        presentator = _get_models(
            serving, run_headers(serving.mint.workload(PRESENTATOR_CLIENT), None)
        )

    assert songmaker.status_code == presentator.status_code == 200
    assert answer_of(songmaker) == answer_of(presentator)


def test_n10_two_instances_of_one_configuration_answer_alike_while_one_has_every_place_taken():
    held = HeldRunner()
    config = _config(runs={"max_parallel_cli_runs": 1})
    with ThreadPoolExecutor() as pool, _serving(config, cli=held) as busy, _serving(config) as idle:
        try:
            pending = pool.submit(busy.person_run, None)
            assert held.entered.wait(TEST_WAIT_SECONDS), "the run was not admitted"
            assert_rejected(busy.person_run(), RejectionCode.BUSY, 429)

            from_busy = _get_models(busy, _workload(busy))
            from_idle = _get_models(idle, _workload(idle))
        finally:
            held.proceed_all()
            pending.result(timeout=TEST_WAIT_SECONDS)

    assert from_busy.status_code == from_idle.status_code == 200
    assert answer_of(from_busy) == answer_of(from_idle)


def test_n11_every_listed_name_is_admitted_as_a_runs_model(tmp_path):
    with _serving(a_config_with_both_providers(tmp_path)) as serving:
        listed = ModelList.model_validate_json(_get_models(serving, _workload(serving)).content)
        started = [_started(serving.person_run(model=entry.id)).model for entry in listed.data]

    assert started == [entry.id for entry in listed.data]


def test_no_token_reaches_a_models_refusal_or_log(caplog):
    with _serving() as serving:
        workload, user = serving.mint.workload(), serving.mint.user()
        with caplog.at_level(DEBUG):
            refusals = [
                _get_models(serving, {"Agent-Workload-Token": workload, "Agent-Job-Ticket": "t"}),
                _get_models(serving, {"Agent-Workload-Token": workload[:-4] + "AAAA"}),
                _get_models(serving, run_headers(workload, user)),
            ]

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for token in (workload, user):
        assert token not in logged
        assert all(token.encode() not in refusal.content for refusal in refusals)
