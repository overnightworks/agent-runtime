"""The gateway as a backend: one buffered chat completion per turn, under one deadline.

The app's key never leaves this module: it is read per turn, sent as the bearer and
written to no exception or log record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from collections.abc import AsyncGenerator
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Final

import httpx2

from agent_runtime_client.agents import TurnRequest
from agent_runtime_client.backend import (
    MAX_RETRY_AFTER_S,
    AgentBackend,
    TurnEvent,
    TurnPerson,
    TurnStopKind,
    TurnStopped,
)
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.turn import MAX_EVENT_BYTES, FailureCode, Principal, RejectionCode
from agent_runtime_client.turn_events import AssistantTextEvent, FinalEvent

_LOGGER: Final = logging.getLogger(__name__)
_COMPLETIONS_PATH: Final = "/v1/chat/completions"
# A DEBUG record of either library may carry request headers, and the key is one.
_TRANSPORT_LOGGERS: Final = ("httpx2", "httpcore2")
_MAX_KEY_BYTES: Final = 1024
_PRINTABLE_ASCII: Final = re.compile(r"[\x21-\x7e]+")
# JSON writes one byte of text as up to six (a \u00XX escape); the rest is the envelope.
_JSON_ESCAPE_FACTOR: Final = 6
_ENVELOPE_ALLOWANCE_BYTES: Final = 65_536
_MAX_BODY_BYTES: Final = _JSON_ESCAPE_FACTOR * MAX_EVENT_BYTES + _ENVELOPE_ALLOWANCE_BYTES


class GatewayDetail(StrEnum):
    """Why a turn stopped, for logs only."""

    KEY_UNUSABLE = "key_unusable"
    GATEWAY_REFUSED = "gateway_refused"
    GATEWAY_FAILED = "gateway_failed"
    CONNECTION_FAILED = "connection_failed"
    MALFORMED_REPLY = "malformed_reply"


class GatewayBackend(AgentBackend):
    def __init__(
        self, config: AgentConfig, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        for name in _TRANSPORT_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        self._config = config
        self._transport = transport

    async def run(
        self, request: TurnRequest, person: TurnPerson
    ) -> AsyncGenerator[TurnEvent, None]:
        if request.tools:
            raise ValueError("tools are not served by the gateway backend yet")
        if person.role not in request.roles:
            raise TurnStopped(TurnStopKind.REFUSED, RejectionCode.FORBIDDEN)
        text = await self._answer(request, person)
        yield AssistantTextEvent(text=text)
        yield FinalEvent(text=text)

    async def _answer(self, request: TurnRequest, person: TurnPerson) -> str:
        try:
            async with asyncio.timeout(request.max_duration_seconds) as deadline:
                if self._config.api_group not in await person.groups():
                    raise TurnStopped(TurnStopKind.REFUSED, RejectionCode.NOT_ENABLED)
                key = _read_key(self._config.gateway.key_file)
                body = _body(request, _fingerprint(person.principal))
                return await self._post(body, key)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise TurnStopped(TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED) from None

    async def _post(self, body: str, key: str) -> str:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            # ssl.SSLError is an OSError.
            async with (
                self._client() as client,
                client.stream("POST", _COMPLETIONS_PATH, content=body, headers=headers) as response,
            ):
                return await _answer_of(response)
        except (httpx2.HTTPError, OSError):
            raise _unavailable(GatewayDetail.CONNECTION_FAILED) from None

    def _client(self) -> httpx2.AsyncClient:
        gateway = self._config.gateway
        verify = True
        if gateway.ca_file is not None and self._transport is None:
            verify = ssl.create_default_context(cafile=str(gateway.ca_file))
        # One client per turn keeps cookies from outliving it; the deadline is the only timeout.
        return httpx2.AsyncClient(
            base_url=gateway.origin,
            transport=self._transport,
            verify=verify,
            trust_env=False,
            follow_redirects=False,
            timeout=None,
        )


def _fingerprint(principal: Principal) -> str:
    """The budget identity of a person: one contract with every app, so a change splits budgets."""
    identity = json.dumps([principal.iss, principal.sub], ensure_ascii=False, separators=(",", ":"))
    return "fp-" + sha256(identity.encode("utf-8")).hexdigest()


def _body(request: TurnRequest, user: str) -> str:
    messages = [{"role": "system", "content": request.turn.system}]
    messages += [
        {"role": message.role.value, "content": message.text} for message in request.turn.messages
    ]
    return json.dumps({"model": request.model, "messages": messages, "user": user})


def _read_key(key_file: Path) -> str:
    try:
        with key_file.open("rb") as file:
            raw = file.read(_MAX_KEY_BYTES + 1)
    except OSError:
        raise _unavailable(GatewayDetail.KEY_UNUSABLE) from None
    line = raw.decode("ascii", errors="replace").removesuffix("\n")
    if len(raw) > _MAX_KEY_BYTES or _PRINTABLE_ASCII.fullmatch(line) is None:
        raise _unavailable(GatewayDetail.KEY_UNUSABLE)
    return line


def _unavailable(detail: GatewayDetail) -> TurnStopped:
    return TurnStopped(TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, detail=detail)


async def _answer_of(response: httpx2.Response) -> str:
    status = response.status_code
    if status == httpx2.codes.OK:
        return await _text_of(response)
    if status == httpx2.codes.TOO_MANY_REQUESTS:
        wait = _wait_seconds(response.headers.get("Retry-After"))
        raise TurnStopped(TurnStopKind.REFUSED, RejectionCode.BUSY, retry_after_s=wait)
    _LOGGER.warning("the gateway answered the turn with status %d", status)
    is_refusal = httpx2.codes.BAD_REQUEST <= status < httpx2.codes.INTERNAL_SERVER_ERROR
    detail = GatewayDetail.GATEWAY_REFUSED if is_refusal else GatewayDetail.GATEWAY_FAILED
    raise _unavailable(detail)


async def _text_of(response: httpx2.Response) -> str:
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > _MAX_BODY_BYTES:
                raise _malformed_reply()
    except httpx2.DecodingError:
        raise _malformed_reply() from None
    try:
        choice = json.loads(body)["choices"][0]
        finish_reason, text = choice["finish_reason"], choice["message"]["content"]
        size = len(text.encode("utf-8"))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
        raise _malformed_reply() from None
    if finish_reason != "stop" or not text.strip() or size > MAX_EVENT_BYTES:
        raise _malformed_reply()
    return text


def _malformed_reply() -> TurnStopped:
    return TurnStopped(
        TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR, detail=GatewayDetail.MALFORMED_REPLY
    )


def _wait_seconds(retry_after: str | None) -> int | None:
    """Delay-seconds only: an HTTP-date, a sign or a missing header gives none."""
    if retry_after is None or not retry_after.isascii() or not retry_after.isdigit():
        return None
    digits = retry_after.lstrip("0") or "0"
    if len(digits) > len(str(MAX_RETRY_AFTER_S)):
        return MAX_RETRY_AFTER_S
    return min(int(digits), MAX_RETRY_AFTER_S)
