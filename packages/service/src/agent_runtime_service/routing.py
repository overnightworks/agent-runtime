"""Which route serves one caller (decision record 0001, H5).

``route`` is the one decider: the CLI route of the model's provider for members of a
subscription group when a ``[cli.<provider>]`` table has that provider, else the API route
for members of an API group, else not enabled. The model is the configured one
(``ServiceConfig.model_for``); the route never chooses it. Membership of the app is
the caller check's gate (``agent_runtime_service.identity``), before any route is chosen.
"""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
from typing import ClassVar

from agent_runtime_client.wire import RouteKind
from agent_runtime_service.config import IdentityConfig, Provider


@dataclass(frozen=True)
class Cli:
    """The subscription route of one provider's program."""

    kind: ClassVar[RouteKind] = RouteKind.CLI
    provider: Provider


@dataclass(frozen=True)
class Api:
    """The paid route."""

    kind: ClassVar[RouteKind] = RouteKind.API


def split_model(model: str) -> tuple[str, str]:
    """The provider and the bare name of a full ``<provider>/<model>``."""
    provider, _separator, name = model.partition("/")
    return provider, name


@dataclass(frozen=True)
class NotEnabled:
    pass


Route = Cli | Api | NotEnabled
ServedRoute = Cli | Api


def _subscription_route(
    identity: IdentityConfig,
    providers: Set[Provider],
    groups: frozenset[str],
    model: str,
) -> Cli | None:
    if identity.subscription_groups.isdisjoint(groups):
        return None
    provider, _name = split_model(model)
    return Cli(Provider(provider)) if provider in providers else None


def route(
    identity: IdentityConfig,
    providers: Set[Provider],
    groups: frozenset[str],
    model: str,
) -> Route:
    subscription = _subscription_route(identity, providers, groups, model)
    if subscription is not None:
        return subscription
    if not identity.api_groups.isdisjoint(groups):
        return Api()
    return NotEnabled()
