"""The person front's ``subscriptions/listen``: changes per verified principal, a level trigger.

A notification only says that something changed: the client reads the resource afterwards, so a
lost, repeated or late one leaves no stale state. An app calls ``ResourceChanges.changed`` only
after the change is visible to a read. Every open stream keeps its own pending URIs, coalesced
and in arrival order, which is why several streams of one principal each receive a change and
no overflow policy exists.

The SDK's ``ListenHandler`` checks no principal, so this module owns the handler. It registers
its stream first, so a change while the acknowledgement is being built is kept, and it checks
every pulse against ``MemberResources.current`` when the pulse is drained, queued pulses included,
so a removal ends delivery during an open stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

import anyio
from mcp.server.context import ServerRequestContext
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY
from mcp_types import (
    ErrorData,
    ResourceUpdatedNotification,
    ResourceUpdatedNotificationParams,
    SubscriptionFilter,
    SubscriptionsAcknowledgedNotification,
    SubscriptionsAcknowledgedNotificationParams,
    SubscriptionsListenRequestParams,
    SubscriptionsListenResult,
)

from agents.mcp_server.asgi import LISTEN_METHOD, LOGGER_NAME
from agents.mcp_server.resources import MemberResources, _failure
from agents.member import (
    LISTEN_ALLOWANCE_SECONDS,
    LISTEN_BOUND_SECONDS,
    STANDING_URI,
    place_ref_from_window_uri,
)
from agents.turn import Principal

_LOG: Final = logging.getLogger(LOGGER_NAME)


class _ListenStream:
    """One listen stream: its pending URIs, a repeated one staying once at its first arrival, and
    the URIs it honors from its acknowledgement on."""

    def __init__(self) -> None:
        self._uris: dict[str, None] = {}
        self._arrived = anyio.Event()
        self._honored: dict[str, None] = {}

    def add(self, uri: str) -> None:
        self._uris[uri] = None
        self._arrived.set()

    async def next(self) -> str:
        while not self._uris:
            await self._arrived.wait()
            self._arrived = anyio.Event()
        uri = next(iter(self._uris))
        del self._uris[uri]
        return uri

    @property
    def honored(self) -> Collection[str]:
        return self._honored.keys()

    def honor(self, uris: Collection[str]) -> None:
        self._honored = dict.fromkeys(uris)


class ResourceChanges:
    """How an app says that a member's resource changed; no argument of a request reaches it.

    ``listening_changed`` is called with the principal, the URI and the new answer of
    ``listens_to`` whenever that answer changes: to yes on the request task that wrote the first
    acknowledgement honoring the URI, to no on the event loop once the last such stream ended and
    ``LISTEN_ALLOWANCE_SECONDS`` passed. It must not block; its failure is logged by type and
    never reaches a listen.
    """

    def __init__(
        self, listening_changed: Callable[[Principal, str, bool], None] | None = None
    ) -> None:
        self._streams: dict[Principal, set[_ListenStream]] = {}
        self._listening_changed = listening_changed

    def changed(self, recipient: Principal, uri: str) -> None:
        """Signal every registered stream of the recipient; a stream that has ended and waits out
        its allowance never reads the pulse.

        Runs on the front's event loop or through ``anyio.from_thread.run_sync``, and only after
        the change is visible to the read.
        """
        for pending in self._streams.get(recipient, ()):
            pending.add(uri)

    def listens_to(self, principal: Principal, uri: str) -> bool:
        """Whether a stream of the principal honored the URI and is open or ended not yet
        ``LISTEN_ALLOWANCE_SECONDS`` ago.

        A stream not yet acknowledged and one honoring only other URIs do not count; the URI
        asked is compared as it is, so a place window is not the standing. Runs on the front's
        event loop, as ``changed`` does.
        """
        return any(uri in stream.honored for stream in self._streams.get(principal, ()))

    @contextmanager
    def opened(self, recipient: Principal) -> Iterator[_ListenStream]:
        """One stream for as long as it is open; one that honored a URI counts on for the
        allowance after it ends."""
        stream = _ListenStream()
        self._streams.setdefault(recipient, set()).add(stream)
        try:
            yield stream
        finally:
            if stream.honored:
                asyncio.get_running_loop().call_later(
                    LISTEN_ALLOWANCE_SECONDS, self._lapsed, recipient, stream
                )
            else:
                self._forget(recipient, stream)

    def acknowledged(
        self, recipient: Principal, stream: _ListenStream, honored: Collection[str]
    ) -> None:
        """The stream's acknowledgement was written: it honors the URIs from now on."""
        newly_listened = [uri for uri in honored if not self.listens_to(recipient, uri)]
        stream.honor(honored)
        for uri in newly_listened:
            self._signal(recipient, uri, listens=True)

    def _lapsed(self, recipient: Principal, stream: _ListenStream) -> None:
        self._forget(recipient, stream)
        for uri in stream.honored:
            if not self.listens_to(recipient, uri):
                self._signal(recipient, uri, listens=False)

    def _forget(self, recipient: Principal, stream: _ListenStream) -> None:
        streams = self._streams[recipient]
        streams.discard(stream)
        if not streams:
            del self._streams[recipient]

    def _signal(self, recipient: Principal, uri: str, *, listens: bool) -> None:
        if self._listening_changed is None:
            return
        try:
            self._listening_changed(recipient, uri, listens)
        except Exception as failure:
            _LOG.error(
                "the listening_changed callable failed: it raised %s", type(failure).__name__
            )


def _wanted(uri: str, honored: Collection[str]) -> bool:
    """Its own URI was honored, or the standing was and the URI is a place window."""
    return uri in honored or (
        STANDING_URI in honored and place_ref_from_window_uri(uri) is not None
    )


def _honored(requested: Sequence[str], readable: Collection[str]) -> list[str]:
    """The requested URIs the principal may read, in request order and without duplicates."""
    return list(dict.fromkeys(uri for uri in requested if uri in readable))


@dataclass(frozen=True)
class _Stream:
    """One request's stream, every frame carrying the subscription id the client chose."""

    context: ServerRequestContext[Any]

    @property
    def _meta(self) -> dict[str, Any]:
        return {SUBSCRIPTION_ID_META_KEY: self.context.request_id}

    async def acknowledge(self, honored: Sequence[str]) -> None:
        await self._send(
            SubscriptionsAcknowledgedNotification(
                params=SubscriptionsAcknowledgedNotificationParams(
                    notifications=SubscriptionFilter(resource_subscriptions=honored or None),
                    _meta=self._meta,
                )
            )
        )

    async def updated(self, uri: str) -> None:
        await self._send(
            ResourceUpdatedNotification(
                params=ResourceUpdatedNotificationParams(uri=uri, _meta=self._meta)
            )
        )

    def completed(self) -> SubscriptionsListenResult:
        return SubscriptionsListenResult(_meta=self._meta)

    async def _send(
        self, notification: ResourceUpdatedNotification | SubscriptionsAcknowledgedNotification
    ) -> None:
        await self.context.session.send_notification(
            notification, related_request_id=self.context.request_id
        )


async def _deliver(
    resources: MemberResources,
    principal: Principal,
    open_stream: _ListenStream,
    stream: _Stream,
) -> None:
    """Writes each wanted pulse the principal may still read, until a port failure ends it."""
    while True:
        uri = await open_stream.next()
        if not _wanted(uri, open_stream.honored):
            continue
        try:
            still_readable = uri in await resources.current(principal)
        except Exception as failure:
            _failure(LISTEN_METHOD, failure)
            return
        if still_readable:
            await stream.updated(uri)


async def on_subscriptions_listen(
    resources: MemberResources,
    changes: ResourceChanges,
    principal: Principal,
    context: ServerRequestContext[Any],
    params: SubscriptionsListenRequestParams,
) -> SubscriptionsListenResult | ErrorData:
    """One stream for the verified principal; it ends at ``LISTEN_BOUND_SECONDS``.

    No exception of the port reaches the SDK; a cancellation propagates.
    """
    stream = _Stream(context)
    with changes.opened(principal) as open_stream, anyio.move_on_after(LISTEN_BOUND_SECONDS):
        try:
            readable = frozenset(await resources.current(principal))
        except Exception as failure:
            return _failure(LISTEN_METHOD, failure)
        honored = _honored(params.notifications.resource_subscriptions or (), readable)
        await stream.acknowledge(honored)
        changes.acknowledged(principal, open_stream, honored)
        await _deliver(resources, principal, open_stream, stream)
    return stream.completed()
