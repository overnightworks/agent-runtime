"""The port through which a member reads an app's current truth, and the person front's answers."""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Final, Protocol

from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    ErrorData,
    ListResourcesResult,
    ReadResourceResult,
    Resource,
    TextResourceContents,
)

from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_wire import JSON_MEDIA_TYPE
from agents.member import MemberStanding, PlaceView, PlaceWindow
from agents.turn import Principal

_LOG: Final = logging.getLogger(LOGGER_NAME)
_RESOURCE_FAILED_TEXT: Final = "the resource failed"
_UNKNOWN_RESOURCE_TEXT: Final = "Unknown resource"


class MemberResources(Protocol):
    """What the app owes the person front: each member's own URIs and the truth behind them.

    The principal is the request's verified one; an app decides participation and permission
    itself, and a URI is no credential.
    """

    async def current(self, principal: Principal) -> Collection[str]:
        """Every URI this principal may read now."""
        ...

    async def read(
        self, principal: Principal, uri: str
    ) -> MemberStanding | PlaceWindow | PlaceView | None:
        """The app's current truth for this URI, or ``None`` when it is not this principal's."""
        ...


async def list_answer(
    resources: MemberResources, principal: Principal
) -> ListResourcesResult | ErrorData:
    """Exactly the URIs the port lists for the principal, sorted."""
    try:
        uris = sorted(await resources.current(principal))
        return ListResourcesResult(
            resources=[Resource(uri=uri, name=uri, mime_type=JSON_MEDIA_TYPE) for uri in uris],
            ttl_ms=0,
            cache_scope="private",
        )
    except Exception as failure:
        return _failure("resources/list", failure)


async def read_answer(
    resources: MemberResources, principal: Principal, uri: str
) -> ReadResourceResult | ErrorData:
    """The port's truth for the URI, or one fixed text that echoes nothing of it."""
    try:
        record = await resources.read(principal, uri)
        if record is None:
            return ErrorData(code=INVALID_PARAMS, message=_UNKNOWN_RESOURCE_TEXT)
        contents = TextResourceContents(
            uri=uri, mime_type=JSON_MEDIA_TYPE, text=record.model_dump_json()
        )
        return ReadResourceResult(contents=[contents], ttl_ms=0, cache_scope="private")
    except Exception as failure:
        return _failure("resources/read", failure)


def _failure(method: str, failure: Exception) -> ErrorData:
    """The fixed answer of a failed port; only the exception's type is logged."""
    _LOG.error("a resource method failed: %s raised %s", method, type(failure).__name__)
    return ErrorData(code=INTERNAL_ERROR, message=_RESOURCE_FAILED_TEXT)
