"""The person front's ``subscriptions/listen``: changes per verified principal, a level trigger.

A notification only says that something changed: the client reads the resource or the tool list
afterwards, so a lost, repeated or late one leaves no stale state. An app calls
``ResourceChanges.changed`` or ``tools_changed`` only after the change is visible to a read. Every
open stream keeps its own pending pulses, coalesced and in arrival order, which is why several
streams of one principal each receive a change and no overflow policy exists.

The SDK's ``ListenHandler`` checks no principal, so this module owns the handler. It registers
its stream first, so a change while the acknowledgement is being built is kept, and it checks
every pulse against ``MemberResources.current`` when the pulse is drained, queued pulses included,
so a removal ends delivery during an open stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

import anyio
from mcp.server.context import ServerRequestContext
from mcp.shared.subscriptions import (
    SUBSCRIPTION_ID_META_KEY,
    ResourceUpdated,
    ToolsListChanged,
    event_to_notification,
)
from mcp_types import (
    ErrorData,
    ServerNotification,
    SubscriptionFilter,
    SubscriptionsAcknowledgedNotification,
    SubscriptionsAcknowledgedNotificationParams,
    SubscriptionsListenRequestParams,
    SubscriptionsListenResult,
)

from agents.mcp_server.asgi import LISTEN_METHOD, LOGGER_NAME, WrittenFrames
from agents.mcp_server.resources import MemberResources, failure_answer, served_uris
from agents.member import (
    LISTEN_ALLOWANCE_SECONDS,
    LISTEN_BOUND_SECONDS,
    STANDING_URI,
    place_ref_from_view_uri,
    place_ref_from_window_uri,
)
from agents.turn import Principal

_LOG: Final = logging.getLogger(LOGGER_NAME)

type _Pulse = ResourceUpdated | ToolsListChanged

_TOOLS_NOTICE_METHOD: Final = event_to_notification(ToolsListChanged(), {}).method.encode("ascii")


class _ListenStream:
    """One listen stream: its pending pulses, a repeated one staying once at its first arrival,
    and what it honors from its acknowledgement on: URIs, and whether the tool list."""

    def __init__(self) -> None:
        self._pulses: dict[_Pulse, None] = {}
        self._arrived = anyio.Event()
        self._honored: dict[str, None] = {}
        self._honors_tools = False
        self._tools_notice_unwritten = False

    def add(self, pulse: _Pulse) -> None:
        if isinstance(pulse, ToolsListChanged) and self._tools_notice_unwritten:
            return
        self._pulses[pulse] = None
        self._arrived.set()

    def writing_tools_notice(self) -> None:
        """From now until its frame is on the wire, a tools change is that notice."""
        self._tools_notice_unwritten = True

    def frame_written(self, frame: bytes) -> None:
        """A frame is on the wire; once it is the tools notice, that notice is out."""
        if _TOOLS_NOTICE_METHOD in frame:
            self._tools_notice_unwritten = False

    async def next(self) -> _Pulse:
        while not self._pulses:
            await self._arrived.wait()
            self._arrived = anyio.Event()
        pulse = next(iter(self._pulses))
        del self._pulses[pulse]
        return pulse

    @property
    def honored(self) -> Collection[str]:
        return self._honored.keys()

    @property
    def honors_tools(self) -> bool:
        return self._honors_tools

    def honor(self, uris: Collection[str], *, tools: bool) -> None:
        self._honored = dict.fromkeys(uris)
        self._honors_tools = tools


class ResourceChanges:
    """How an app says that a member's resource or tool offer changed; no argument of a request
    reaches it.

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
            pending.add(ResourceUpdated(uri))

    def tools_changed(self, recipient: Principal) -> None:
        """Signal every registered stream of the recipient that the tools offered to it changed;
        only a stream that honored ``toolsListChanged`` writes it.

        Runs on the front's event loop or through ``anyio.from_thread.run_sync``, and only after
        the new offer is visible to ``offered_tools``.
        """
        for pending in self._streams.get(recipient, ()):
            pending.add(ToolsListChanged())

    def listens_to(self, principal: Principal, uri: str) -> bool:
        """Whether a stream of the principal honored the URI and is open or ended not yet
        ``LISTEN_ALLOWANCE_SECONDS`` ago.

        A stream not yet acknowledged and one honoring only other URIs do not count; the URI
        asked is compared as it is, so a place window is not the standing, and a stream that honored
        only the tool list counts for no URI. Runs on the front's event loop, as ``changed``
        does.
        """
        return any(uri in stream.honored for stream in self._streams.get(principal, ()))

    @contextmanager
    def opened(self, recipient: Principal) -> Generator[_ListenStream, None, None]:
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
        self,
        recipient: Principal,
        stream: _ListenStream,
        honored: Collection[str],
        *,
        tools: bool,
    ) -> None:
        """The stream's acknowledgement was written: it honors the URIs, and the tool list when
        ``tools``, from now on."""
        newly_listened = [uri for uri in honored if not self.listens_to(recipient, uri)]
        stream.honor(honored, tools=tools)
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
    """Its own URI was honored, or the standing was and the URI is a place window or view."""
    return uri in honored or (
        STANDING_URI in honored
        and (place_ref_from_window_uri(uri) is not None or place_ref_from_view_uri(uri) is not None)
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

    async def acknowledge(self, honored: Sequence[str], *, tools: bool) -> None:
        await self._send(
            SubscriptionsAcknowledgedNotification(
                params=SubscriptionsAcknowledgedNotificationParams(
                    notifications=SubscriptionFilter(
                        tools_list_changed=True if tools else None,
                        resource_subscriptions=list(honored) or None,
                    ),
                    _meta=self._meta,
                )
            )
        )

    async def pulse(self, pulse: _Pulse) -> None:
        await self._send(event_to_notification(pulse, self._meta))

    def completed(self) -> SubscriptionsListenResult:
        return SubscriptionsListenResult(_meta=self._meta)

    async def _send(
        self, notification: ServerNotification | SubscriptionsAcknowledgedNotification
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
    """Writes each wanted pulse, a resource's only while the principal may still read it, until a
    port failure ends it."""
    while True:
        pulse = await open_stream.next()
        if isinstance(pulse, ToolsListChanged):
            if open_stream.honors_tools:
                open_stream.writing_tools_notice()
                await stream.pulse(pulse)
            continue
        if not _wanted(pulse.uri, open_stream.honored):
            continue
        try:
            still_readable = pulse.uri in await served_uris(resources, principal)
        except Exception as failure:
            failure_answer(LISTEN_METHOD, failure)
            return
        if still_readable:
            await stream.pulse(pulse)


async def on_subscriptions_listen(
    resources: MemberResources,
    changes: ResourceChanges,
    principal: Principal,
    context: ServerRequestContext[Any],
    params: SubscriptionsListenRequestParams,
    frames: WrittenFrames,
    *,
    announces_tools: bool,
) -> SubscriptionsListenResult | ErrorData:
    """One stream for the verified principal; it ends at ``LISTEN_BOUND_SECONDS``.

    ``toolsListChanged`` is honored when asked and ``announces_tools``; no other list-changed
    flag is. No exception of the port reaches the SDK; a cancellation propagates.
    """
    stream = _Stream(context)
    with changes.opened(principal) as open_stream, anyio.move_on_after(LISTEN_BOUND_SECONDS):
        try:
            readable = await served_uris(resources, principal)
        except Exception as failure:
            return failure_answer(LISTEN_METHOD, failure)
        frames.watch(open_stream.frame_written)
        honored = _honored(params.notifications.resource_subscriptions or (), readable)
        tools = announces_tools and params.notifications.tools_list_changed is True
        await stream.acknowledge(honored, tools=tools)
        changes.acknowledged(principal, open_stream, honored, tools=tools)
        await _deliver(resources, principal, open_stream, stream)
    return stream.completed()
