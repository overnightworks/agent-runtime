"""A run may name a model (decision record 0001, the model-per-run ruling, protocol 5).

The configuration owns what is offered (``ServiceConfig.offered_models()``) and the default; a
request names an offered model or none. Every test goes through the HTTP entrance with real
token checks, on the person's path and on the app's own path.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

import httpx
import pytest
from tests.support.bounds import TEST_WAIT_SECONDS
from tests.support.held_runner_support import HeldRunner
from tests.support.service_support import (
    API_GROUP,
    COWRITER,
    SONGMAKER_GROUP,
    SONGMAKER_TOOLS,
    SUBSCRIPTION_GROUP,
    ScriptedRunner,
    TokenMint,
    assert_rejected,
    decoded_events,
    post_run,
    run_body,
    run_headers,
    served,
    service_config,
)
from tests.support.service_table_support import CODEX_NAME, a_config_with_both_providers
from webauth.https_client import KeySetFetchError

from agent_runtime.events import AssistantTextEvent, FinalEvent
from agent_runtime_client.wire import RejectionCode, RunStarted
from agent_runtime_service.app import create_app
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.routing import Api, Cli
from agent_runtime_service.runners import Runners

MINT = TokenMint()
CLI_SCRIPT = (AssistantTextEvent(text="via the CLI"), FinalEvent(text="CLI answer"))
API_SCRIPT = (AssistantTextEvent(text="via the API"), FinalEvent(text="API answer"))
SONNET = "anthropic/sonnet"
OPUS = "anthropic/opus"
HAIKU = "anthropic/haiku"
NOT_OFFERED = "anthropic/unlisted"
CLAUDE_MODELS = ("sonnet", "opus", "haiku")
PERSON = [SUBSCRIPTION_GROUP, SONGMAKER_GROUP]
API_ONLY_PERSON = [API_GROUP, SONGMAKER_GROUP]
NO_ROUTE_PERSON = [SONGMAKER_GROUP]
RUNS_AS_APP = {COWRITER: {"runs_as_app": True}}


def _config(songmaker: Mapping[str, object] | None = None) -> ServiceConfig:
    return service_config(songmaker=songmaker, claude_models=CLAUDE_MODELS)


@dataclass(frozen=True)
class _Service:
    base_url: str
    cli: ScriptedRunner
    api: ScriptedRunner

    def person_run(self, groups: list[str] = PERSON, **body: object) -> httpx.Response:
        headers = run_headers(MINT.workload(), MINT.user(groups=groups))
        return post_run(self.base_url, headers, run_body(**body))

    def app_run(self, **body: object) -> httpx.Response:
        return post_run(
            self.base_url,
            run_headers(MINT.workload(groups=[SUBSCRIPTION_GROUP]), None),
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
    cli_script=CLI_SCRIPT,
    cli=None,
    fetch_key_set=MINT.fetch_key_set,
) -> Iterator[_Service]:
    cli, api = cli or ScriptedRunner(cli_script), ScriptedRunner(API_SCRIPT)
    app = create_app(
        config or _config(), runners=Runners(cli=cli, api=api), fetch_key_set=fetch_key_set
    )
    with served(app) as base_url:
        yield _Service(base_url, cli, api)


@pytest.fixture
def service() -> Iterator[_Service]:
    with _serving(_config({"agents": RUNS_AS_APP})) as serving:
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
            {"model": HAIKU, "agents": {COWRITER: {"model": OPUS}}}, OPUS, id="the-agents"
        ),
        pytest.param({"model": HAIKU}, HAIKU, id="the-apps"),
        pytest.param({}, SONNET, id="the-default"),
    ],
)
@pytest.mark.parametrize("named", [pytest.param(False, id="absent"), pytest.param(True, id="null")])
def test_m1_to_m4_a_run_naming_no_model_runs_the_configured_one(songmaker, expected, named):
    body = {"model": None} if named else {}
    with _serving(_config(songmaker)) as serving:
        started = _started(serving.person_run(**body))

    assert (started.model, serving.model_run) == (expected, expected)


def test_m5_a_named_offered_model_wins_over_the_configured_levels():
    config = _config({"model": HAIKU, "agents": {COWRITER: {"model": OPUS}}})
    with _serving(config) as serving:
        started = _started(serving.person_run(model=SONNET))

    assert (started.model, serving.model_run) == (SONNET, SONNET)


def test_m6_a_model_of_another_provider_runs_on_that_providers_route(tmp_path):
    with _serving(a_config_with_both_providers(tmp_path)) as serving:
        started = _started(serving.person_run(model=CODEX_NAME))

    assert started.model == CODEX_NAME
    assert serving.cli.admitted[-1].route == Cli(CODEX_NAME.split("/")[0])


@pytest.mark.parametrize("model", [NOT_OFFERED, "openai/unlisted"])
def test_m7_a_model_that_is_not_offered_is_refused_and_starts_nothing(service, model):
    response = service.person_run(model=model)

    assert_rejected(response, RejectionCode.MODEL_NOT_OFFERED, 422)
    assert service.started == 0


@pytest.mark.parametrize("model", ["Opus", "anthropic", "anthropic/opus@1", "ANTHROPIC/opus", ""])
def test_m8_a_malformed_model_is_an_invalid_request(service, model):
    assert_rejected(service.person_run(model=model), RejectionCode.INVALID_REQUEST, 422)


def test_m9_the_apps_own_run_may_name_an_offered_model(service):
    started = _started(service.app_run(model=OPUS))

    assert (started.model, service.model_run) == (OPUS, OPUS)


def test_m10_the_apps_own_run_naming_a_model_that_is_not_offered_is_refused(service):
    assert_rejected(service.app_run(model=NOT_OFFERED), RejectionCode.MODEL_NOT_OFFERED, 422)
    assert service.started == 0


def test_m11_a_person_without_the_role_is_forbidden_before_the_model_is_checked(service):
    response = service.person_run(model=NOT_OFFERED, roles=["admin"])

    assert_rejected(response, RejectionCode.FORBIDDEN, 403)


def test_m12_tools_without_an_mcp_origin_are_forbidden_before_the_model_is_checked():
    config = service_config(songmaker={"mcp_origin": None}, claude_models=CLAUDE_MODELS)
    with _serving(config) as serving:
        response = serving.person_run(model=NOT_OFFERED, tools=SONGMAKER_TOOLS)

    assert_rejected(response, RejectionCode.FORBIDDEN, 403)


def test_m13_an_invalid_user_token_is_unauthorized_whatever_model_is_named(service):
    headers = run_headers(MINT.workload(), "not-a-token")

    response = post_run(service.base_url, headers, run_body(model=NOT_OFFERED))

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)


def test_m14_an_unreachable_key_set_is_unavailable_whatever_model_is_named():
    def unreachable_issuer(_url: str) -> bytes:
        raise KeySetFetchError("the issuer cannot be reached")

    with _serving(fetch_key_set=unreachable_issuer) as serving:
        response = serving.person_run(model=NOT_OFFERED)

    assert_rejected(response, RejectionCode.UNAVAILABLE, 503)


def test_m15_a_job_ticket_header_is_unauthorized_before_any_token_is_read(service):
    headers = {"Agent-Job-Ticket": "ticket", **run_headers(MINT.workload(), None)}

    response = post_run(service.base_url, headers, run_body(model=NOT_OFFERED))

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)


def test_m16_an_api_only_caller_names_an_offered_model_on_the_api_route(service):
    started = _started(service.person_run(API_ONLY_PERSON, model=OPUS))

    assert started.model == OPUS
    assert (service.api.admitted[-1].route, service.api.admitted[-1].model) == (Api(), OPUS)
    assert service.cli.admitted == []


def test_m17_a_caller_in_no_route_group_is_not_enabled_though_the_model_is_offered(service):
    response = service.person_run(NO_ROUTE_PERSON, model=OPUS)

    assert_rejected(response, RejectionCode.NOT_ENABLED, 403)
    assert service.started == 0


def test_m18_tools_on_a_codex_model_are_not_enabled(tmp_path):
    with _serving(a_config_with_both_providers(tmp_path)) as serving:
        response = serving.person_run(model=CODEX_NAME, tools=SONGMAKER_TOOLS)

    assert_rejected(response, RejectionCode.NOT_ENABLED, 403)
    assert serving.started == 0


def test_m19_a_full_subscription_answers_busy_and_never_falls_back_to_the_api_route():
    held = HeldRunner()
    config = service_config(runs={"max_parallel_cli_runs": 1}, claude_models=CLAUDE_MODELS)
    both_routes = [SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP]
    with ThreadPoolExecutor() as pool, _serving(config, cli=held) as serving:
        try:
            pending = pool.submit(serving.person_run, both_routes, model=OPUS)
            assert held.entered.wait(TEST_WAIT_SECONDS), "the run was not admitted"

            busy = serving.person_run(both_routes, model=HAIKU)

            assert_rejected(busy, RejectionCode.BUSY, 429)
            assert serving.api.admitted == []
        finally:
            held.proceed_all()
            pending.result(timeout=TEST_WAIT_SECONDS)


@pytest.mark.parametrize("model", [{"model": OPUS}, {}])
def test_m20_a_body_of_protocol_4_is_unsupported(service, model):
    response = service.person_run(v=4, **model)

    assert_rejected(response, RejectionCode.UNSUPPORTED_VERSION, 400)


def test_m25_a_model_whose_run_fails_falls_back_to_no_other_model():
    with _serving(_config(), cli_script=[RuntimeError("provider error")]) as serving:
        events = decoded_events(serving.person_run(model=OPUS).content)

    assert [(event.type, getattr(event, "model", None)) for event in events][0] == (
        "run_started",
        OPUS,
    )
    assert events[-1].type != "succeeded"
    assert [run.model for run in serving.cli.admitted] == [OPUS]
    assert serving.api.admitted == []
