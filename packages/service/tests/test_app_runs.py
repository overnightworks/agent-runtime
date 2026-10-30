"""An app's own agent runs on its workload token alone (decision record 0001, H3 and H12).

A request with neither ``Authorization`` nor ``Agent-Job-Ticket`` is the app's own run. It is
admitted only for an agent the operator enabled with ``[apps.<app>.agents.<agent>]
runs_as_app = true`` and runs as the app's service account, routed by that account's groups.
Any ``Authorization`` header, even an empty one, keeps the request on the person's path.
Every test goes through the HTTP entrance with real token checks.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx
import pytest
import uvicorn
from tests.support.bounds import TEST_WAIT_SECONDS
from tests.support.held_runner_support import HeldRunner
from tests.support.service_config_support import (
    MCP_SECTION,
    SIGNING_SECTION,
    VALID_CONFIG,
    assert_startup_refuses,
    refused_on_one_line,
    written,
)
from tests.support.service_support import (
    API_GROUP,
    COWRITER,
    LISTED_MODEL,
    PRESENTATOR_CLIENT,
    PRESENTER,
    SONGMAKER_CLIENT,
    SONGMAKER_GROUP,
    SUBSCRIPTION_GROUP,
    TEST_ISSUER,
    ScriptedRunner,
    TokenMint,
    WallClock,
    assert_rejected,
    decoded_events,
    get_run,
    post_run,
    run_body,
    run_headers,
    served,
    service_config,
)
from tests.support.service_table_support import CODEX_NAME, a_config_with_both_providers
from tests.support.token_rejection_support import UNKNOWN_RUN
from webauth.https_client import KeySetFetchError

from agent_runtime.events import AssistantTextEvent, FinalEvent
from agent_runtime_client.wire import RejectionCode, RouteKind, RunStarted
from agent_runtime_service.app import create_app
from agent_runtime_service.config import AgentConfig, Provider, ServiceConfig, load_config
from agent_runtime_service.routing import Api, Cli
from agent_runtime_service.runners import Runners

MINT = TokenMint()
OTHER_MINT = TokenMint(key_id="another-signing-key")
CLI_SCRIPT = (AssistantTextEvent(text="via the CLI"), FinalEvent(text="CLI answer"))
API_SCRIPT = (AssistantTextEvent(text="via the API"), FinalEvent(text="API answer"))
SERVICE_ACCOUNT = f"service-account-user-of-{SONGMAKER_CLIENT}"
LISTED = f"{Provider.ANTHROPIC}/{LISTED_MODEL}"
ENABLED_COWRITER: Final = {COWRITER: {"runs_as_app": True}}


def _config(
    songmaker_agents: Mapping[str, object] = ENABLED_COWRITER,
    presentator_agents: Mapping[str, bool] | None = None,
    runs: Mapping[str, object] | None = None,
) -> ServiceConfig:
    config = service_config(runs=runs, songmaker={"agents": songmaker_agents})
    if presentator_agents is None:
        return config
    presentator = config.apps["presentator"].model_copy(
        update={
            "agents": {
                agent: AgentConfig(runs_as_app=enabled)
                for agent, enabled in presentator_agents.items()
            }
        }
    )
    return config.model_copy(update={"apps": {**config.apps, "presentator": presentator}})


@dataclass(frozen=True)
class _Service:
    base_url: str
    cli: ScriptedRunner
    api: ScriptedRunner

    def start(self, headers: Mapping[str, str], **body: object) -> httpx.Response:
        return post_run(self.base_url, headers, run_body(**body))

    def start_raw(self, headers: Mapping[str, str], content: bytes) -> httpx.Response:
        return post_run(self.base_url, headers, content=content)

    @property
    def started(self) -> int:
        return len(self.cli.admitted) + len(self.api.admitted)


@contextmanager
def _serving(
    config: ServiceConfig | None = None,
    *,
    fetch_key_set=MINT.fetch_key_set,
    wall_clock: WallClock | None = None,
    cli=None,
) -> Iterator[_Service]:
    cli_runner = cli or ScriptedRunner(CLI_SCRIPT)
    api_runner = ScriptedRunner(API_SCRIPT)
    app = create_app(
        config or _config(),
        runners=Runners(cli=cli_runner, api=api_runner),
        fetch_key_set=fetch_key_set,
        **({} if wall_clock is None else {"wall_clock": wall_clock}),
    )
    with served(app) as base_url:
        yield _Service(base_url, cli_runner, api_runner)


@pytest.fixture
def service() -> Iterator[_Service]:
    with _serving() as serving:
        yield serving


def _own(
    groups: object = (SUBSCRIPTION_GROUP,), client: str = SONGMAKER_CLIENT, **claims: object
) -> dict[str, str]:
    """The headers of an app's own run: the workload token alone."""
    shown = list(groups) if isinstance(groups, tuple) else groups
    return run_headers(MINT.workload(client, groups=shown, **claims), None)


def _person(**user_claims: object) -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user(**user_claims))


def _workload_of_lifetime(seconds: int) -> str:
    issued_at = int(time.time())
    return MINT.workload(groups=[SUBSCRIPTION_GROUP], iat=issued_at, exp=issued_at + seconds)


def _expired_workload() -> str:
    now = int(time.time())
    return MINT.workload(iat=now - 400, exp=now - 100)


def _run_id_of(response: httpx.Response) -> str:
    started = decoded_events(response.content)[0]
    assert isinstance(started, RunStarted)
    return str(started.run_id)


def _succeeded(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text
    assert decoded_events(response.content)[-1].type == "succeeded"


def test_p1_e1_a_workload_token_alone_runs_an_enabled_agent_as_the_service_account(service):
    response = service.start(_own())

    _succeeded(response)
    (admitted,) = service.cli.admitted
    assert (admitted.app, admitted.principal.issuer, admitted.principal.subject) == (
        "songmaker",
        TEST_ISSUER,
        SERVICE_ACCOUNT,
    )
    assert (admitted.route, admitted.model) == (Cli(Provider.ANTHROPIC), LISTED)


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param("", id="p2-empty"),
        pytest.param("Basic dXNlcjpwYXNz", id="p3-basic"),
        pytest.param("Bearer", id="p3-bearer-without-a-token"),
        pytest.param("Bearer two words", id="p3-a-token-with-a-space"),
        pytest.param("garbage", id="p3-no-scheme"),
    ],
)
@pytest.mark.parametrize("route", ["POST", "GET"])
def test_p2_p3_o6_an_authorization_header_that_is_no_bearer_token_is_never_the_apps_own_run(
    service, authorization, route
):
    headers = {**_own(), "Authorization": authorization}

    response = (
        service.start(headers)
        if route == "POST"
        else get_run(service.base_url, headers, UNKNOWN_RUN)
    )

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)
    assert service.started == 0


@pytest.mark.parametrize(
    "user_token",
    [
        pytest.param(lambda: MINT.user(iat=1, exp=2), id="p4-expired"),
        pytest.param(lambda: OTHER_MINT.user(), id="p5-unknown-key"),
        pytest.param(lambda: MINT.user() + "x", id="p5-bad-signature"),
    ],
)
@pytest.mark.parametrize("route", ["POST", "GET"])
def test_p4_p5_o6_a_bearer_token_that_does_not_verify_is_unauthorized_not_the_apps_own_run(
    service, user_token, route
):
    headers = {**_own(), "Authorization": f"Bearer {user_token()}"}

    response = (
        service.start(headers)
        if route == "POST"
        else get_run(service.base_url, headers, UNKNOWN_RUN)
    )

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)
    assert service.started == 0


@pytest.mark.parametrize(
    "user_claims",
    [
        pytest.param({"groups": [SUBSCRIPTION_GROUP]}, id="not-in-the-app"),
        pytest.param({"azp": PRESENTATOR_CLIENT}, id="azp-mismatch"),
        pytest.param({"sub": SERVICE_ACCOUNT}, id="the-workloads-own-subject"),
    ],
)
def test_p6_a_valid_but_forbidden_bearer_token_is_forbidden_not_the_apps_own_run(
    service, user_claims
):
    headers = {**_own(), "Authorization": f"Bearer {MINT.user(**user_claims)}"}

    assert_rejected(service.start(headers), RejectionCode.FORBIDDEN, 403)
    assert service.started == 0


def test_p7_a_person_without_the_requested_role_is_forbidden_though_the_agent_runs_as_the_app(
    service,
):
    refused = service.start(_person(groups=[SONGMAKER_GROUP]), roles=["admin"])

    assert_rejected(refused, RejectionCode.FORBIDDEN, 403)
    assert service.started == 0


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"sub": None}, id="without-a-subject"),
        pytest.param({"sub": "service-\ud800"}, id="subject-with-a-lone-surrogate"),
        pytest.param({"groups": "/subscription-members"}, id="malformed-groups"),
        pytest.param(
            {"sub": None, "realm_access": {"roles": []}}, id="without-a-subject-and-not-a-workload"
        ),
        pytest.param(
            {"sub": "service-\ud800", "client_id": PRESENTATOR_CLIENT},
            id="lone-surrogate-and-client-id-differs-from-azp",
        ),
        pytest.param(
            {"groups": "/subscription-members", "realm_access": {"roles": []}},
            id="malformed-groups-and-not-a-workload",
        ),
        pytest.param(
            {"groups": "/subscription-members", "client": "unknown-app"},
            id="malformed-groups-and-unregistered-client",
        ),
    ],
)
@pytest.mark.parametrize("route", ["POST", "GET"])
def test_an_own_token_with_an_unreadable_identity_is_unauthorized_before_any_forbidden_fault(
    service, claims, route
):
    claims = dict(claims)
    client = claims.pop("client", SONGMAKER_CLIENT)
    headers = _own(client=client, **claims)

    response = (
        service.start(headers)
        if route == "POST"
        else get_run(service.base_url, headers, UNKNOWN_RUN)
    )

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)
    assert service.started == 0


@pytest.mark.parametrize("ticket", ["", "garbage", "a.b.c"])
def test_p8_a_job_ticket_header_is_refused_even_beside_the_apps_own_credentials(service, ticket):
    refused = service.start({**_own(), "Agent-Job-Ticket": ticket})

    assert_rejected(refused, RejectionCode.UNAUTHORIZED, 401)
    assert service.started == 0


def _unreachable_issuer(_url: str) -> bytes:
    raise KeySetFetchError("the issuer cannot be reached")


def test_p9_an_unreachable_key_set_is_unavailable_for_the_apps_own_run():
    with _serving(fetch_key_set=_unreachable_issuer) as unreachable:
        refused = unreachable.start(_own())

        assert_rejected(refused, RejectionCode.UNAVAILABLE, 503)


@pytest.mark.parametrize(
    "make_headers",
    [
        pytest.param(lambda: {}, id="w1-no-token"),
        pytest.param(lambda: {"Agent-Workload-Token": "garbage"}, id="w2-garbage"),
        pytest.param(lambda: {"Agent-Workload-Token": _expired_workload()}, id="w2-expired"),
        pytest.param(
            lambda: {"Agent-Workload-Token": OTHER_MINT.workload()}, id="w2-unknown-key"
        ),
    ],
)
def test_w1_w2_a_missing_or_unverifiable_workload_token_is_unauthorized(service, make_headers):
    assert_rejected(service.start(make_headers()), RejectionCode.UNAUTHORIZED, 401)
    assert service.started == 0


@pytest.mark.parametrize(
    "workload_token",
    [
        pytest.param(
            lambda: _workload_of_lifetime(301),
            id="lifetime-over-300-seconds",
        ),
        pytest.param(
            lambda: MINT.workload(groups=[SUBSCRIPTION_GROUP], realm_access={"roles": []}),
            id="no-agent-workload-role",
        ),
        pytest.param(
            lambda: MINT.workload(groups=[SUBSCRIPTION_GROUP], azp=PRESENTATOR_CLIENT),
            id="azp-is-not-the-client-id",
        ),
        pytest.param(
            lambda: MINT.workload("stranger-test", groups=[SUBSCRIPTION_GROUP]),
            id="unregistered-client",
        ),
        pytest.param(lambda: MINT.user(), id="a-user-token-as-the-workload-token"),
    ],
)
def test_w3_a_token_that_does_not_bind_a_registered_app_is_forbidden(service, workload_token):
    refused = service.start({"Agent-Workload-Token": workload_token()})

    assert_rejected(refused, RejectionCode.FORBIDDEN, 403)
    assert service.started == 0


def test_w4_a_workload_subject_with_a_lone_surrogate_is_unauthorized_and_signs_nothing(service):
    refused = service.start(_own(sub="service-\ud800"))

    assert_rejected(refused, RejectionCode.UNAUTHORIZED, 401)
    assert service.started == 0


@pytest.mark.parametrize("groups", ["/subscription-members", [SUBSCRIPTION_GROUP, 1]])
def test_w5_a_group_claim_that_is_no_list_of_strings_is_unauthorized(service, groups):
    assert_rejected(service.start(_own(groups)), RejectionCode.UNAUTHORIZED, 401)
    assert service.started == 0


@pytest.mark.parametrize("groups", [None, []], ids=["absent", "empty"])
def test_w6_a_service_account_in_no_group_is_not_enabled_after_the_body(service, groups):
    assert_rejected(service.start(_own(groups)), RejectionCode.NOT_ENABLED, 403)
    assert_rejected(service.start_raw(_own(groups), b"{"), RejectionCode.INVALID_REQUEST, 422)
    assert service.started == 0


@pytest.mark.parametrize(
    "agents",
    [
        pytest.param({}, id="no-table"),
        pytest.param({COWRITER: {"runs_as_app": False}}, id="disabled"),
        pytest.param({"assistant": {"runs_as_app": True}}, id="another-agent-only"),
    ],
)
def test_e2_an_agent_without_an_enabled_table_is_forbidden_after_the_body(agents):
    enabled_elsewhere = {"other-agent": {"runs_as_app": True}}
    with _serving(_config({**agents, **enabled_elsewhere})) as serving:
        assert_rejected(serving.start(_own()), RejectionCode.FORBIDDEN, 403)
        assert_rejected(serving.start_raw(_own(), b"{"), RejectionCode.INVALID_REQUEST, 422)
        assert serving.started == 0


@pytest.mark.parametrize(
    "agents",
    [{}, {COWRITER: {"runs_as_app": False}}],
    ids=["no-agents", "none-enabled"],
)
@pytest.mark.parametrize("route", ["POST", "GET"])
def test_e3_o5_an_app_with_no_agent_that_runs_as_itself_is_unauthorized_before_the_body(
    agents, route
):
    with _serving(_config(agents)) as serving:
        response = (
            serving.start_raw(_own(), b"{")
            if route == "POST"
            else get_run(serving.base_url, _own(), UNKNOWN_RUN)
        )

        assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)
        assert serving.started == 0


def test_e4_each_apps_own_tables_decide_for_its_own_workload_token():
    presentator = _own(client=PRESENTATOR_CLIENT)
    with _serving(_config(presentator_agents={})) as without_tables:
        assert_rejected(without_tables.start(presentator), RejectionCode.UNAUTHORIZED, 401)
    with _serving(_config(presentator_agents={PRESENTER: True})) as with_tables:
        assert_rejected(
            with_tables.start(presentator, agent=COWRITER), RejectionCode.FORBIDDEN, 403
        )
        _succeeded(with_tables.start(presentator, agent=PRESENTER))
        assert with_tables.cli.admitted[-1].app == "presentator"


def test_e5_the_roles_of_the_body_are_not_read_for_the_apps_own_run(service):
    _succeeded(service.start(_own(), roles=["admin"]))


@pytest.mark.parametrize(
    ("groups", "app_model", "agent_model", "route", "model"),
    [
        pytest.param([SUBSCRIPTION_GROUP], None, None, Cli(Provider.ANTHROPIC), LISTED, id="r1"),
        pytest.param([API_GROUP], None, CODEX_NAME, Api(), CODEX_NAME, id="r2-the-agents-model"),
        pytest.param([API_GROUP], CODEX_NAME, None, Api(), CODEX_NAME, id="r3-app"),
        pytest.param(
            [SUBSCRIPTION_GROUP, API_GROUP], CODEX_NAME, LISTED, Cli(Provider.ANTHROPIC), LISTED,
            id="r5-the-agent-over-the-app",
        ),
        pytest.param(
            [SUBSCRIPTION_GROUP, API_GROUP], None, CODEX_NAME, Cli(Provider.OPENAI), CODEX_NAME,
            id="r6-the-subscriber-of-the-models-provider",
        ),
    ],
)
def test_r1_r2_r3_r5_the_route_and_model_come_from_the_service_account_and_the_configuration(
    tmp_path, groups, app_model, agent_model, route, model
):
    config = a_config_with_both_providers(tmp_path)
    songmaker = config.apps["songmaker"].model_copy(
        update={
            "model": app_model,
            "agents": {COWRITER: AgentConfig(runs_as_app=True, model=agent_model)},
        }
    )
    config = config.model_copy(update={"apps": {**config.apps, "songmaker": songmaker}})

    with _serving(config) as service:
        response = service.start(_own(groups))
        _succeeded(response)

    served_by = service.cli if isinstance(route, Cli) else service.api
    assert (served_by.admitted[-1].route, served_by.admitted[-1].model) == (route, model)
    started = decoded_events(response.content)[0]
    assert (started.model, started.route) == (model, route.kind)


@pytest.mark.parametrize(
    ("groups", "route"),
    [
        pytest.param([SONGMAKER_GROUP, SUBSCRIPTION_GROUP], RouteKind.CLI, id="cli"),
        pytest.param([SONGMAKER_GROUP, API_GROUP], RouteKind.API, id="api"),
    ],
)
def test_a_persons_run_starts_naming_the_configured_model_and_its_route(service, groups, route):
    response = service.start(_person(groups=groups))

    _succeeded(response)
    started = decoded_events(response.content)[0]
    assert (started.model, started.route) == (LISTED, route)


def test_r4_a_service_account_in_no_route_group_is_not_enabled(service):
    refused = service.start(_own([SONGMAKER_GROUP]))

    assert_rejected(refused, RejectionCode.NOT_ENABLED, 403)
    assert service.started == 0


@pytest.mark.parametrize(
    ("holder", "asker"),
    [
        pytest.param(_person, lambda: _own([SUBSCRIPTION_GROUP, API_GROUP]), id="s1"),
        pytest.param(_own, _person, id="s2"),
    ],
)
def test_s1_s2_the_apps_own_runs_and_people_share_the_subscription_places_without_fallback(
    holder, asker
):
    held = HeldRunner()
    with (
        ThreadPoolExecutor() as pool,
        _serving(_config(runs={"max_parallel_cli_runs": 1}), cli=held) as serving,
    ):
        try:
            pending = pool.submit(serving.start, holder())
            assert held.entered.wait(TEST_WAIT_SECONDS), "the run was not admitted"

            busy = serving.start(asker())

            assert_rejected(busy, RejectionCode.BUSY, 429)
            assert serving.api.admitted == []
        finally:
            held.proceed_all()
        assert pending.result(TEST_WAIT_SECONDS).status_code == 200


def test_o1_the_workload_token_alone_reads_the_apps_own_run(service):
    run_id = _run_id_of(service.start(_own()))

    status = get_run(service.base_url, _own(), run_id)

    assert status.status_code == 200, status.text
    assert status.json()["state"] == "succeeded"


def test_o2_people_of_the_same_app_cannot_read_the_apps_own_run(service):
    run_id = _run_id_of(service.start(_own()))

    assert_rejected(get_run(service.base_url, _person(), run_id), RejectionCode.NOT_FOUND, 404)


def test_o3_the_workload_token_alone_cannot_read_a_persons_run(service):
    run_id = _run_id_of(service.start(_person()))

    assert_rejected(get_run(service.base_url, _own(), run_id), RejectionCode.NOT_FOUND, 404)


def test_o4_another_apps_workload_token_alone_cannot_read_the_run():
    with _serving(_config(presentator_agents={PRESENTER: True})) as serving:
        run_id = _run_id_of(serving.start(_own()))

        other = get_run(serving.base_url, _own(client=PRESENTATOR_CLIENT), run_id)

        assert_rejected(other, RejectionCode.NOT_FOUND, 404)
    with _serving() as without_tables:
        run_id = _run_id_of(without_tables.start(_own()))

        other = get_run(without_tables.base_url, _own(client=PRESENTATOR_CLIENT), run_id)

        assert_rejected(other, RejectionCode.UNAUTHORIZED, 401)


def test_c1_the_capability_of_the_apps_own_run_names_the_service_account_and_floored_times():
    clock = WallClock()
    clock.set_to(datetime(2026, 10, 16, 12, 0, 0, 700_000, tzinfo=UTC))
    with _serving(_config(runs={"max_duration_s": 300.5}), wall_clock=clock) as serving:
        _succeeded(serving.start(_own()))

        capability = serving.cli.admitted[-1].capability
    assert capability is not None
    claims = capability.claims
    now = clock().timestamp()
    assert (claims.principal.iss, claims.principal.sub) == (TEST_ISSUER, SERVICE_ACCOUNT)
    assert (claims.app, claims.agent) == ("songmaker", COWRITER)
    assert (claims.iat, claims.exp) == (math.floor(now), math.floor(now + 300.5))


def test_an_apps_agent_table_names_the_agents_that_run_as_the_app(tmp_path):
    text = (
        VALID_CONFIG
        + "\n[apps.songmaker.agents.cowriter]\nruns_as_app = true\n"
        + "\n[apps.songmaker.agents.notes]\n"
    )

    songmaker = load_config(written(tmp_path, text)).apps["songmaker"]

    assert songmaker.runs_as_itself("cowriter")
    assert not songmaker.runs_as_itself("notes")
    assert not songmaker.runs_as_itself("unnamed")


@pytest.mark.parametrize(
    ("text", "named_field"),
    [
        pytest.param(
            VALID_CONFIG + "\n[apps.songmaker.agents.Co_Writer]\nruns_as_app = true\n",
            "apps.songmaker.agents.Co_Writer",
            id="E6-an-agent-named-by-an-invalid-agent",
        ),
        pytest.param(
            VALID_CONFIG + '\n[apps.songmaker.agents.cowriter]\nruns_as_app = "yes"\n',
            "apps.songmaker.agents.cowriter.runs_as_app",
            id="E6-a-runs-as-app-that-is-no-boolean",
        ),
        pytest.param(
            VALID_CONFIG + '\n[apps.songmaker.agents.cowriter]\nroute = "api"\n',
            "apps.songmaker.agents.cowriter.route",
            id="E6-an-unknown-key-in-the-agent-table",
        ),
    ],
)
def test_a_refused_agent_table_stops_startup_naming_the_field(tmp_path, capsys, text, named_field):
    assert_startup_refuses(tmp_path, capsys, text, named_field)


def test_a_configuration_without_an_app_stops_startup_naming_apps(tmp_path, capsys, monkeypatch):
    def never_serve(*_arguments, **_where):
        raise AssertionError("a refused configuration reached serving")

    monkeypatch.setattr(uvicorn, "run", never_serve)
    no_apps = VALID_CONFIG.split("[apps.songmaker]")[0] + "[apps]\n"
    text = no_apps.replace(SIGNING_SECTION, "").replace(MCP_SECTION, "")

    assert_startup_refuses(tmp_path, capsys, text, "apps")
    assert refused_on_one_line(written(tmp_path, text), capsys).startswith("apps")
