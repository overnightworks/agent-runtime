"""The run routes refuse an ``Agent-Job-Ticket`` header before any token is verified or the
body is read.

The header is refused whatever its value, on ``POST /runs`` and ``GET /runs/{id}``, ahead of
the token checks and the body; the retired ``POST /job-tickets`` route is gone.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import httpx
import pytest
from tests.support.service_support import (
    ScriptedRunner,
    TokenMint,
    asgi_post_answer,
    assert_rejected,
    get_run,
    post_run,
    run_headers,
    served,
    service_config,
    unused_runners,
)
from webauth.https_client import KeySetFetchError

from agent_runtime.events import FinalEvent
from agent_runtime_client.wire import RejectionCode
from agent_runtime_service.app import create_app
from agent_runtime_service.run_records import RunRecorder, RunRecords
from agent_runtime_service.runners import Runners

MINT = TokenMint()
UNKNOWN_RUN = "00000000-0000-4000-8000-000000000000"


class _WatchedRecords(RunRecords):
    def __init__(self) -> None:
        super().__init__()
        self.begun: list[uuid.UUID] = []

    def begin(self, started, app, principal) -> RunRecorder:
        self.begun.append(started.run_id)
        return super().begin(started, app, principal)


def _unreachable_issuer(_url: str) -> bytes:
    raise KeySetFetchError("the issuer cannot be reached")


@pytest.fixture
def runner() -> ScriptedRunner:
    return ScriptedRunner((FinalEvent(text="answer"),))


@pytest.fixture
def records() -> _WatchedRecords:
    return _WatchedRecords()


@pytest.fixture
def service(runner, records) -> Iterator[str]:
    app = create_app(
        service_config(),
        runners=Runners(cli=runner, api=runner),
        records=records,
        fetch_key_set=MINT.fetch_key_set,
    )
    with served(app) as base_url:
        yield base_url


def _send(base_url: str, route: str, headers: dict[str, str]) -> httpx.Response:
    if route == "post-runs":
        return post_run(base_url, headers, content=b"not json")
    return get_run(base_url, headers, UNKNOWN_RUN)


@pytest.mark.parametrize("ticket", ["", "garbage"], ids=["empty", "garbage"])
@pytest.mark.parametrize("route", ["post-runs", "get-run"])
def test_a_job_ticket_header_is_unauthorized_on_both_run_routes(service, route, ticket):
    headers = {**run_headers(MINT.workload(), MINT.user()), "Agent-Job-Ticket": ticket}

    response = _send(service, route, headers)

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)


@pytest.mark.parametrize(
    ("ticket", "code", "status"),
    [
        pytest.param("", RejectionCode.UNAUTHORIZED, 401, id="empty-header"),
        pytest.param("garbage", RejectionCode.UNAUTHORIZED, 401, id="header-with-a-value"),
        pytest.param(None, RejectionCode.UNAVAILABLE, 503, id="no-header"),
    ],
)
@pytest.mark.parametrize("with_authorization", [False, True], ids=["bare", "with-authorization"])
@pytest.mark.parametrize("route", ["post-runs", "get-run"])
def test_the_header_is_refused_before_any_token_is_verified(
    route, with_authorization, ticket, code, status
):
    headers = run_headers(MINT.workload(), MINT.user() if with_authorization else None)
    if ticket is not None:
        headers["Agent-Job-Ticket"] = ticket
    app = create_app(
        service_config(), runners=unused_runners(), fetch_key_set=_unreachable_issuer
    )

    with served(app) as base_url:
        response = _send(base_url, route, headers)

    assert_rejected(response, code, status)


@pytest.mark.parametrize("ticket", ["", "garbage"], ids=["empty", "garbage"])
@pytest.mark.parametrize("with_authorization", [False, True], ids=["bare", "with-authorization"])
@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/runs"), ("GET", f"/runs/{UNKNOWN_RUN}")], ids=["post", "get"]
)
def test_the_header_is_refused_without_reading_the_request_body(
    method, path, with_authorization, ticket
):
    headers = {
        **run_headers(MINT.workload(), MINT.user() if with_authorization else None),
        "Agent-Job-Ticket": ticket,
    }

    async def receive() -> dict[str, object]:
        pytest.fail("the request body was read")

    app = create_app(
        service_config(), runners=unused_runners(), fetch_key_set=MINT.fetch_key_set
    )

    status, _ = asyncio.run(asgi_post_answer(app, headers, receive, (method, path)))

    assert status == 401


@pytest.mark.parametrize("ticket", ["", "garbage"], ids=["empty", "garbage"])
def test_a_valid_run_with_a_job_ticket_header_starts_no_run(service, runner, records, ticket):
    headers = {**run_headers(MINT.workload(), MINT.user()), "Agent-Job-Ticket": ticket}

    response = post_run(service, headers)

    assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)
    assert runner.admitted == []
    assert records.begun == []


def test_the_job_ticket_route_is_gone(service):
    response = httpx.post(
        f"{service}/job-tickets", headers=run_headers(MINT.workload(), MINT.user()), json={}
    )

    assert response.status_code == 404
