"""The HTTP surface: ``POST /runs``, the one entrance through which an app starts a run,
and ``GET /runs/{id}``, through which the app and user that started it read where it stands.

Both routes check the two tokens first, with the same answers. ``POST /runs`` and
``GET /runs/{id}`` answer a request carrying an ``Agent-Job-Ticket`` header ``unauthorized``
before any token is verified or the body is read; the header's value is never read. A
``POST`` is then checked in a fixed order, and the first failure answers:

1. the caller check, webauth's ``ServiceCallerCheck`` (``agent_runtime_service.identity``),
   before the body is read or anything else is decided: both tokens verified, their subjects
   and group claims read (``unauthorized`` or ``unavailable``), then the workload binding, the
   user's client binding and membership of the app (``/apps/<app>`` or a path below it);
2. the body's framing, then its protocol version, then its shape
   (``agent_runtime_service.run_request``);
3. the person's role in the app (``admin`` in ``/apps/<app>/admin`` or below, else ``user``)
   must be one of the body's ``roles``, else ``forbidden``;
4. tools named by an app without an MCP origin answer ``forbidden``, before the route;
5. the model: the request's, when it names one, else the configured one
   (``ServiceConfig.model_for``); a name the installation does not list
   (``ServiceConfig.offered_models``) answers ``model_not_offered``;
6. the route (``agent_runtime_service.routing``), which reads that model and the
   installation's listed models; a run that names tools and whose route is a provider without
   the tool relay (Codex, Grok) answers ``not_enabled`` here, before a place, a capability or
   a child;
7. for the CLI route, the service's admission limit (``agent_runtime_service.admission``): a
   subscription run beyond it answers ``busy`` with ``Retry-After`` before anything is
   signed;
8. for an app with an MCP origin, whether the run's capability, which signs exactly the
   request's tool names, fits the 8,192 characters every verifier accepts
   (``capability_too_large``).

An admitted run answers ``200 text/event-stream`` with its frames
(``agent_runtime_service.run_events``), and its response closes those frames whenever it
ends, however it ends, and hands its runner one capability when its app has an MCP
origin. Every refusal is a typed ``RunRejection``
that is never cached. No answer echoes a token. A ``GET /runs/{id}`` answers the run's
``RunStatus`` to its owner, and one ``not_found`` to everyone else: for an id
nobody began, an expired or evicted run, another app's or user's run, and a
path segment that is not a run id's text form.

``GET /models`` answers the ``ModelList`` of ``ServiceConfig.offered_models`` to the workload
token of any registered app, whether or not one of its agents runs as the app. It reads no
``Authorization`` header and no run state, so every instance of one configuration answers the
same bytes; a refusal is the caller check's, after the job-ticket refusal.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from datetime import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send
from webauth.https_client import fetch_key_set_over_https
from webauth.jwt_verification import KeySetFetch, MonotonicClock
from webauth.service_callers import WorkloadCaller

from agent_runtime import lost_groups
from agent_runtime_client.capability import MAX_CAPABILITY_CHARACTERS
from agent_runtime_client.wire import (
    PROTOCOL_VERSION,
    ModelList,
    ModelListEntry,
    ModelName,
    RejectionCode,
    RunRejection,
    RunRequest,
)
from agent_runtime_service.admission import AdmissionSlot, Lease
from agent_runtime_service.capability import (
    Capability,
    CapabilityScope,
    CapabilityTooLarge,
    capability_claims,
    signed_capability,
)
from agent_runtime_service.config import RELAY_ATTACHED_PROVIDERS, ServiceConfig
from agent_runtime_service.identity import Caller, CallerCheck, PersonCaller, agent_role
from agent_runtime_service.quarantine import GroupConfirmation, Quarantine
from agent_runtime_service.routing import Cli, NotEnabled, ServedRoute, route
from agent_runtime_service.run_events import run_frames
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.run_records import RunRecords, utc_now
from agent_runtime_service.run_request import MAX_REQUEST_BYTES, parse_run_request
from agent_runtime_service.runners import AdmittedRun, Runners

if TYPE_CHECKING:
    from starlette.types import Lifespan

    from agent_runtime_service.renewal_schedule import RenewalSchedule

WORKLOAD_TOKEN_HEADER: Final = "Agent-Workload-Token"  # noqa: S105 - a header name and no secret
JOB_TICKET_HEADER: Final = "Agent-Job-Ticket"
RETRY_UNAVAILABLE_AFTER_SECONDS: Final = 30
RETRY_BUSY_AFTER_SECONDS: Final = 10
_BEARER_SCHEME: Final = "bearer"
_JSON_MEDIA_TYPE: Final = "application/json"

_log = logging.getLogger(__name__)

_STATUS: Final[Mapping[RejectionCode, HTTPStatus]] = {
    RejectionCode.NOT_ENABLED: HTTPStatus.FORBIDDEN,
    RejectionCode.BUSY: HTTPStatus.TOO_MANY_REQUESTS,
    RejectionCode.UNSUPPORTED_VERSION: HTTPStatus.BAD_REQUEST,
    RejectionCode.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    RejectionCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
    RejectionCode.INVALID_REQUEST: HTTPStatus.UNPROCESSABLE_ENTITY,
    RejectionCode.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    RejectionCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    RejectionCode.CAPABILITY_TOO_LARGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    RejectionCode.MODEL_NOT_OFFERED: HTTPStatus.UNPROCESSABLE_ENTITY,
}

_MESSAGE: Final[Mapping[RejectionCode, str]] = {
    RejectionCode.NOT_ENABLED: "AI is not enabled for this request.",
    RejectionCode.BUSY: "The service is busy; retry later.",
    RejectionCode.UNSUPPORTED_VERSION: f"This service speaks protocol version {PROTOCOL_VERSION}.",
    RejectionCode.UNAUTHORIZED: "A workload token is required; a run also needs a user token unless"
    " the app runs one of its own agents, and every token a run sends must be valid."
    f" An {JOB_TICKET_HEADER} header is refused.",
    RejectionCode.FORBIDDEN: "The tokens do not permit this request.",
    RejectionCode.INVALID_REQUEST: "The body is not a valid request for the route.",
    RejectionCode.UNAVAILABLE: "The service cannot check tokens right now; retry later.",
    RejectionCode.NOT_FOUND: "No such run.",
    RejectionCode.CAPABILITY_TOO_LARGE: "This run's capability would exceed"
    f" {MAX_CAPABILITY_CHARACTERS} characters.",
    RejectionCode.MODEL_NOT_OFFERED: "The service does not offer the model the request names.",
}


def create_app(
    config: ServiceConfig,
    *,
    runners: Runners,
    records: RunRecords | None = None,
    fetch_key_set: KeySetFetch = fetch_key_set_over_https,
    monotonic_clock: MonotonicClock = time.monotonic,
    wall_clock: Callable[[], datetime] = utc_now,
    confirm_group_gone: GroupConfirmation = lost_groups.confirm_group_gone,
    renewals: RenewalSchedule | None = None,
) -> FastAPI:
    """The service's ASGI app.

    The caller check (webauth's ``ServiceCallerCheck``) is built here once, from ``config``;
    ``fetch_key_set`` and ``monotonic_clock`` are its injected key-set fetch and clock.
    ``wall_clock`` (aware UTC) stamps each capability's issue and expiry and is the default
    run-record store's clock. An injected
    ``records`` keeps its own clock, so the two can differ; ``wall_clock`` never reaches the
    caller check, which reads ``time.time``.
    ``confirm_group_gone`` is how the one quarantine asks whether a lost process group is gone.
    ``renewals`` runs while the app's lifespan does; without it the app renews nothing.
    """
    callers = CallerCheck(config, fetch_key_set, monotonic_clock)
    installed_records = records or RunRecords(wall_clock)
    subscription_slot = AdmissionSlot(config.runs.max_parallel_cli_runs)
    quarantine = Quarantine(LoopClock(), confirm_group_gone)
    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan_of(renewals)
    )

    @app.post("/runs")
    async def start_run(request: Request) -> Response:
        caller = await _run_caller_of(callers, request)
        if isinstance(caller, RejectionCode):
            return rejection(caller)
        body = await _limited_body(request)
        run_request = RejectionCode.INVALID_REQUEST if body is None else parse_run_request(body)
        if isinstance(run_request, RejectionCode):
            return rejection(run_request)
        return _admitted(
            config,
            runners,
            installed_records,
            subscription_slot,
            quarantine,
            wall_clock,
            caller,
            run_request,
        )

    app.get("/runs/{run_id}")(_run_status_route(callers, installed_records))
    app.get("/models")(_models_route(callers, _model_list_of(config)))

    return app


def _lifespan_of(renewals: RenewalSchedule | None) -> Lifespan[FastAPI] | None:
    if renewals is None:
        return None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with renewals.running():
            yield

    return lifespan


class _RunResponse(StreamingResponse):
    """A run's response, which closes the run's frames itself whenever it ends.

    Starlette never closes the generator it streams, and a generator left
    paused behind a blocked send is only finalized by the garbage collector.

    It also releases the run's lease on exit, after the frames are closed. That
    frees the place only for a run that never began, because the lease is
    released once the run is over, however long the connection hangs on, and
    a lease the quarantine took over is not this one.
    """

    def __init__(self, frames: AsyncGenerator[bytes, None], lease: Lease | None) -> None:
        super().__init__(
            frames, media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )
        self._lease = lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with aclosing(self.body_iterator):
                await super().__call__(scope, receive, send)
        finally:
            _release(self._lease)


def _admitted(
    config: ServiceConfig,
    runners: Runners,
    records: RunRecords,
    subscription_slot: AdmissionSlot,
    quarantine: Quarantine,
    wall_clock: Callable[[], datetime],
    caller: Caller,
    run_request: RunRequest,
) -> Response:
    if not _may_run(config, caller, run_request):
        return rejection(RejectionCode.FORBIDDEN)
    scope = config.capability_scope(caller.app, run_request.tools)
    if run_request.tools and scope is None:
        return rejection(RejectionCode.FORBIDDEN)
    if run_request.model is None:
        model = config.model_for(caller.app, run_request.agent)
    elif run_request.model in config.offered_models():
        model = run_request.model
    else:
        return rejection(RejectionCode.MODEL_NOT_OFFERED)
    served = route(
        config.identity,
        config.subscription_models().keys(),
        caller.groups,
        model,
    )
    if isinstance(served, NotEnabled) or (
        isinstance(served, Cli)
        and run_request.tools
        and served.provider not in RELAY_ATTACHED_PROVIDERS
    ):
        return rejection(RejectionCode.NOT_ENABLED)
    lease = None
    if isinstance(served, Cli):
        lease = subscription_slot.try_take()
        if lease is None:
            return rejection(RejectionCode.BUSY)
    try:
        response = _admitted_response(
            config,
            runners,
            records,
            lease,
            quarantine,
            wall_clock,
            caller,
            run_request,
            model,
            served,
            scope,
        )
    except BaseException:
        _release(lease)
        raise
    if isinstance(response, RejectionCode):
        _release(lease)
        return rejection(response)
    return response


def _may_run(config: ServiceConfig, caller: Caller, run_request: RunRequest) -> bool:
    if isinstance(caller, WorkloadCaller):
        return config.apps[caller.app].runs_as_itself(run_request.agent)
    return agent_role(caller) in run_request.roles


def _release(lease: Lease | None) -> None:
    if lease is not None:
        lease.release()


def _admitted_response(
    config: ServiceConfig,
    runners: Runners,
    records: RunRecords,
    lease: Lease | None,
    quarantine: Quarantine,
    wall_clock: Callable[[], datetime],
    caller: Caller,
    run_request: RunRequest,
    model: ModelName,
    served: ServedRoute,
    scope: CapabilityScope | None,
) -> _RunResponse | RejectionCode:
    run_id = uuid.uuid4()
    try:
        capability = _capability_of(config, scope, caller, run_request, run_id, wall_clock)
    except CapabilityTooLarge as too_large:
        _log.warning(
            "The capability of a run of app %s is %d characters; the limit is %d.",
            caller.app,
            too_large.length,
            MAX_CAPABILITY_CHARACTERS,
        )
        return RejectionCode.CAPABILITY_TOO_LARGE
    admitted = AdmittedRun(
        run_id=run_id,
        app=caller.app,
        principal=caller.principal,
        request=run_request,
        model=model,
        route=served,
        capability=capability,
    )
    return _RunResponse(
        run_frames(
            admitted,
            runners.for_route(served),
            config.runs,
            LoopClock(),
            records,
            lease,
            quarantine=quarantine,
        ),
        lease,
    )


def _capability_of(
    config: ServiceConfig,
    scope: CapabilityScope | None,
    caller: Caller,
    run_request: RunRequest,
    run_id: UUID,
    wall_clock: Callable[[], datetime],
) -> Capability | None:
    if scope is None:
        return None
    claims = capability_claims(
        scope,
        run_id=run_id,
        app=caller.app,
        issuer=caller.principal.issuer,
        subject=caller.principal.subject,
        agent=run_request.agent,
        now=wall_clock().timestamp(),
        max_duration_s=config.runs.max_duration_s,
    )
    return signed_capability(scope, claims)


def _run_status_route(
    callers: CallerCheck, records: RunRecords
) -> Callable[[Request, str], Awaitable[Response]]:
    async def run_status(request: Request, run_id: str) -> Response:
        caller = await _run_caller_of(callers, request)
        if isinstance(caller, RejectionCode):
            return rejection(caller)
        parsed_id = _run_id_from(run_id)
        status = (
            None if parsed_id is None else records.read(parsed_id, caller.app, caller.principal)
        )
        if status is None:
            return rejection(RejectionCode.NOT_FOUND)
        return Response(
            content=status.model_dump_json(),
            media_type=_JSON_MEDIA_TYPE,
            headers={"Cache-Control": "no-store"},
        )

    return run_status


def _model_list_of(config: ServiceConfig) -> ModelList:
    return ModelList(
        v=PROTOCOL_VERSION,
        data=[ModelListEntry(id=name) for name in sorted(config.offered_models())],
    )


def _models_route(
    callers: CallerCheck, offer: ModelList
) -> Callable[[Request], Awaitable[Response]]:
    """``GET /models``: the offer to the workload token of any registered app, nothing else read."""
    body = offer.model_dump_json()

    async def models(request: Request) -> Response:
        if JOB_TICKET_HEADER in request.headers:
            return rejection(RejectionCode.UNAUTHORIZED)
        caller = await callers.workload_caller(request.headers.get(WORKLOAD_TOKEN_HEADER))
        if isinstance(caller, RejectionCode):
            return rejection(caller)
        return Response(
            content=body, media_type=_JSON_MEDIA_TYPE, headers={"Cache-Control": "no-store"}
        )

    return models


def rejection(code: RejectionCode) -> Response:
    headers = {"Cache-Control": "no-store"}
    if code is RejectionCode.UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    if code is RejectionCode.UNAVAILABLE:
        headers["Retry-After"] = str(RETRY_UNAVAILABLE_AFTER_SECONDS)
    if code is RejectionCode.BUSY:
        headers["Retry-After"] = str(RETRY_BUSY_AFTER_SECONDS)
    body = RunRejection(v=PROTOCOL_VERSION, code=code, message=_MESSAGE[code])
    return Response(
        content=body.model_dump_json(),
        status_code=_STATUS[code],
        media_type=_JSON_MEDIA_TYPE,
        headers=headers,
    )


async def _caller_of(callers: CallerCheck, request: Request) -> PersonCaller | RejectionCode:
    return await callers.caller(
        request.headers.get(WORKLOAD_TOKEN_HEADER),
        _bearer_token(request.headers.get("Authorization")),
    )


async def _run_caller_of(callers: CallerCheck, request: Request) -> Caller | RejectionCode:
    """The caller of a run route, chosen by which headers are present, never by their validity.

    A job-ticket header is refused whatever its value. Any ``Authorization`` header, even an
    empty or malformed one, is the person's path and never falls through; without one, the
    workload token alone is the app's own run.
    """
    if JOB_TICKET_HEADER in request.headers:
        return RejectionCode.UNAUTHORIZED
    if "Authorization" in request.headers:
        return await _caller_of(callers, request)
    return await callers.app_caller(request.headers.get(WORKLOAD_TOKEN_HEADER))


def _run_id_from(segment: str) -> UUID | None:
    """The run id a path segment names: only the 8-4-4-4-12 text form, in any letter case."""
    try:
        run_id = UUID(segment)
    except ValueError:
        return None
    return run_id if str(run_id) == segment.lower() else None


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != _BEARER_SCHEME or not token or " " in token:
        return None
    return token


async def _limited_body(request: Request) -> bytes | None:
    """The body, or ``None`` once it is known to exceed ``MAX_REQUEST_BYTES``.

    A declared length over the limit is refused unread, and a body without
    one is refused at the first chunk that would pass the limit, before that
    chunk is buffered.
    """
    declared = request.headers.get("Content-Length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST_BYTES):
        return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            return None
        body += chunk
    return bytes(body)
