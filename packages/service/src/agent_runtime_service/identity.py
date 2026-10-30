"""Who is asking: the app's workload token and, for a person's run, its end user's token.

The proof is webauth's ``ServiceCallerCheck`` (``webauth.service_callers``), built once per
process from ``[identity]``, the registered apps of ``[apps]`` and the ruled constants of
decision record 0001 (U1'): the realm role ``agent-workload`` and a workload token that
lives at most 300 seconds. It owns the one verifier and the order of the refusals: every
refusal about the tokens that is ``unavailable`` or ``unauthorized`` comes before any
``forbidden`` one (decision record 0001, the order of checks). This module maps each refusal
by its class: a key set that cannot be fetched is ``unavailable``; a missing or unverifiable
token, or a subject or group claim webauth cannot read, is ``unauthorized``; tokens that do
not bind one registered app to one end user, or a user who is no member of the app, are
``forbidden``.

The service keeps its policy: the wire codes, the wire role of a member, and the refusal of an
own run by an app none of whose agents may run as the app (``unauthorized``, after the proof).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Final

import anyio.to_thread
from webauth.jwt_verification import KeySetFetch, MonotonicClock
from webauth.oidc_login import AppRole
from webauth.service_callers import (
    CallerRefusal,
    CallerRefusalReason,
    PersonCaller,
    RegisteredApp,
    ServiceCallerCheck,
    ServiceIdentity,
    WorkloadCaller,
)

from agent_runtime_client.wire import AgentRole, RejectionCode
from agent_runtime_service.config import ServiceConfig

WORKLOAD_ROLE: Final = "agent-workload"
MAX_WORKLOAD_TOKEN_SECONDS: Final = 300

Caller = WorkloadCaller | PersonCaller

_AGENT_ROLE_OF: Final[Mapping[AppRole, AgentRole]] = {
    AppRole.ADMIN: "admin",
    AppRole.USER: "user",
}

_CODE_OF_REASON: Final[Mapping[CallerRefusalReason, RejectionCode]] = {
    CallerRefusalReason.KEY_SET_UNAVAILABLE: RejectionCode.UNAVAILABLE,
    CallerRefusalReason.MISSING_TOKEN: RejectionCode.UNAUTHORIZED,
    CallerRefusalReason.TOKEN_REJECTED: RejectionCode.UNAUTHORIZED,
    CallerRefusalReason.INVALID_SUBJECT: RejectionCode.UNAUTHORIZED,
    CallerRefusalReason.MALFORMED_GROUPS: RejectionCode.UNAUTHORIZED,
    CallerRefusalReason.NOT_A_WORKLOAD: RejectionCode.FORBIDDEN,
    CallerRefusalReason.UNBOUND_CLIENT: RejectionCode.FORBIDDEN,
    CallerRefusalReason.UNREGISTERED_CLIENT: RejectionCode.FORBIDDEN,
    CallerRefusalReason.WORKLOAD_LIVES_TOO_LONG: RejectionCode.FORBIDDEN,
    CallerRefusalReason.CLIENT_MISMATCH: RejectionCode.FORBIDDEN,
    CallerRefusalReason.SAME_SUBJECT: RejectionCode.FORBIDDEN,
    CallerRefusalReason.NOT_A_MEMBER: RejectionCode.FORBIDDEN,
}


def rejection_code(refusal: CallerRefusal) -> RejectionCode:
    """The wire code of a refusal; a reason this service does not know raises ``KeyError``."""
    return _CODE_OF_REASON[refusal.reason]


def agent_role(caller: PersonCaller) -> AgentRole:
    """The wire role of the person's membership in the app."""
    return _AGENT_ROLE_OF[caller.role]


class CallerCheck:
    def __init__(
        self,
        config: ServiceConfig,
        fetch_key_set: KeySetFetch,
        monotonic_clock: MonotonicClock,
    ) -> None:
        identity = config.identity
        self._config = config
        self._check = ServiceCallerCheck(
            ServiceIdentity(
                issuer=identity.issuer,
                audience=identity.audience,
                jwks_url=identity.jwks_url,
                workload_role=WORKLOAD_ROLE,
                group_claim=identity.group_claim,
                max_workload_seconds=MAX_WORKLOAD_TOKEN_SECONDS,
                leeway_seconds=identity.leeway_seconds,
            ),
            registered_apps={
                app.workload_client: RegisteredApp(app_name, config.app_admission(app_name))
                for app_name, app in config.apps.items()
            },
            fetch_key_set=fetch_key_set,
            wall_clock=time.time,
            monotonic_clock=monotonic_clock,
        )

    async def caller(
        self, workload_token: str | None, user_token: str | None
    ) -> PersonCaller | RejectionCode:
        proven = await anyio.to_thread.run_sync(
            self._check.person_caller, workload_token, user_token
        )
        return rejection_code(proven) if isinstance(proven, CallerRefusal) else proven

    async def workload_caller(self, workload_token: str | None) -> WorkloadCaller | RejectionCode:
        """The registered app behind a workload token, else the refusal."""
        proven = await anyio.to_thread.run_sync(self._check.workload_caller, workload_token)
        return rejection_code(proven) if isinstance(proven, CallerRefusal) else proven

    async def app_caller(self, workload_token: str | None) -> WorkloadCaller | RejectionCode:
        """The app behind a workload token that runs one of its own agents, else the refusal."""
        proven = await self.workload_caller(workload_token)
        if isinstance(proven, RejectionCode):
            return proven
        if not self._config.apps[proven.app].has_an_agent_that_runs_as_itself:
            return RejectionCode.UNAUTHORIZED
        return proven
