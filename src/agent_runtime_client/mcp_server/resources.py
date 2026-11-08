"""The port through which a member reads an app's current truth, per verified principal."""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from agent_runtime_client.member import MemberStanding, PlaceWindow
from agent_runtime_client.turn import Principal


class MemberResources(Protocol):
    """What the app owes the person front: each member's own URIs and the truth behind them.

    The principal is the request's verified one; an app decides participation and permission
    itself, and a URI is no credential.
    """

    async def current(self, principal: Principal) -> Collection[str]:
        """Every URI this principal may read now."""
        ...

    async def read(self, principal: Principal, uri: str) -> MemberStanding | PlaceWindow | None:
        """The app's current truth for this URI, or ``None`` when it is not this principal's."""
        ...
