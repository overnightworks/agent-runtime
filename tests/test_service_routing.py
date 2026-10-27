"""H5 routing from the caller's groups (decision record 0001).

The subscription groups and the API groups decide the route; the agent never does, and
membership of the app is the caller check's gate, before any route is chosen. Test values:
subscription groups ``/old-sub`` and ``/new-sub``, API group ``/api-members`` and a Claude
table; every run names the model the configuration chose for it.
"""

from __future__ import annotations

import pytest

from agent_runtime_service.config import IdentityConfig, Provider
from agent_runtime_service.routing import Api, Cli, NotEnabled, route

IDENTITY = IdentityConfig(
    issuer="https://keycloak.test/realms/agent-test",
    jwks_url="https://keycloak.test/realms/agent-test/protocol/openid-connect/certs",
    audience="agent-service",
    subscription_groups=frozenset({"/old-sub", "/new-sub"}),
    api_groups=frozenset({"/api-members"}),
    group_claim="groups",
)
TABLES = frozenset({Provider.ANTHROPIC})
MODEL = f"{Provider.ANTHROPIC}/sonnet"
CLI = Cli(Provider.ANTHROPIC)
API = Api()
OFF = NotEnabled()


def _routed(groups, *, tables=TABLES, identity=IDENTITY, model=MODEL):
    return route(identity, tables, frozenset(groups), model)


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        pytest.param({"/new-sub"}, CLI, id="R1-new-subscription-group"),
        pytest.param({"/old-sub"}, CLI, id="R2-stale-token-keeps-its-route"),
        pytest.param({"/old-sub", "/new-sub"}, CLI, id="R3-both-names"),
        pytest.param({"/new-sub", "/api-members"}, CLI, id="R4-subscription-wins"),
        pytest.param({"/api-members"}, API, id="R7-api-member"),
        pytest.param({"/new-sub/friends"}, OFF, id="R12-subscription-subgroup"),
        pytest.param({"/new-sub-x"}, OFF, id="R12-subscription-look-alike"),
        pytest.param({"/api-members/friends"}, OFF, id="R12a-api-subgroup"),
        pytest.param({"/api-members-x"}, OFF, id="R12a-api-look-alike"),
        pytest.param(set(), OFF, id="R13-no-groups"),
    ],
)
def test_the_route_follows_the_groups(groups, expected):
    assert _routed(groups) == expected


def test_a_stale_subscription_name_is_not_enabled_once_the_configuration_drops_it():
    after_the_cut_over = IDENTITY.model_copy(
        update={"subscription_groups": frozenset({"/new-sub"})}
    )

    assert _routed({"/old-sub"}, identity=after_the_cut_over) == OFF


SUBSCRIBER = {"/new-sub"}
API_ONLY = {"/api-members"}
BOTH = {"/new-sub", "/api-members"}


@pytest.mark.parametrize(
    ("groups", "model", "expected"),
    [
        pytest.param(SUBSCRIBER, f"{Provider.ANTHROPIC}/sonnet", CLI, id="R-a-S"),
        pytest.param(API_ONLY, f"{Provider.ANTHROPIC}/sonnet", Api(), id="R-a-A"),
        pytest.param(BOTH, f"{Provider.ANTHROPIC}/sonnet", CLI, id="R-a-D"),
        pytest.param(SUBSCRIBER, "xai/sonnet", OFF, id="R-d-S"),
        pytest.param(BOTH, "xai/sonnet", Api(), id="R-d-D"),
        pytest.param(set(), f"{Provider.ANTHROPIC}/sonnet", OFF, id="R-e-N"),
    ],
)
def test_a_named_model_runs_on_the_subscription_only_when_a_table_has_its_provider(
    groups, model, expected
):
    assert _routed(groups, model=model) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        pytest.param(f"{Provider.ANTHROPIC}/sonnet", CLI, id="anthropic-model"),
        pytest.param(f"{Provider.OPENAI}/gpt-5.5", Cli(Provider.OPENAI), id="openai-model"),
    ],
)
def test_a_model_goes_to_the_table_of_its_provider(model, expected):
    tables = frozenset({Provider.ANTHROPIC, Provider.OPENAI})

    assert _routed(SUBSCRIBER, tables=tables, model=model) == expected
