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

from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

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

from agents.mcp_server.asgi import LISTEN_METHOD
from agents.mcp_server.resources import MemberResources, _failure
from agents.member import (
    LISTEN_BOUND_SECONDS,
    STANDING_URI,
    place_ref_from_window_uri,
)
from agents.turn import Principal


class _PendingUris:
    """One open stream's changed URIs: a repeated URI stays once, at its first arrival."""

    def __init__(self) -> None:
        self._uris: dict[str, None] = {}
        self._arrived = anyio.Event()

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


class ResourceChanges:
    """How an app says that a member's resource changed; no argument of a request reaches it."""

    def __init__(self) -> None:
        self._streams: dict[Principal, set[_PendingUris]] = {}

    def changed(self, recipient: Principal, uri: str) -> None:
        """Signal every open stream of the recipient.

        Runs on the front's event loop or through ``anyio.from_thread.run_sync``, and only after
        the change is visible to the read.
        """
        for pending in self._streams.get(recipient, ()):
            pending.add(uri)

    @contextmanager
    def opened(self, recipient: Principal) -> Iterator[_PendingUris]:
        """One stream's pending URIs for as long as the stream is open."""
        pending = _PendingUris()
        self._streams.setdefault(recipient, set()).add(pending)
        try:
            yield pending
        finally:
            streams = self._streams[recipient]
            streams.discard(pending)
            if not streams:
                del self._streams[recipient]


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
    pending: _PendingUris,
    honored: Collection[str],
    stream: _Stream,
) -> None:
    """Writes each wanted pulse the principal may still read, until a port failure ends it."""
    while True:
        uri = await pending.next()
        if not _wanted(uri, honored):
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
    with changes.opened(principal) as pending, anyio.move_on_after(LISTEN_BOUND_SECONDS):
        try:
            readable = frozenset(await resources.current(principal))
        except Exception as failure:
            return _failure(LISTEN_METHOD, failure)
        honored = _honored(params.notifications.resource_subscriptions or (), readable)
        await stream.acknowledge(honored)
        await _deliver(resources, principal, pending, frozenset(honored), stream)
    return stream.completed()
