"""H5 routing from the caller's groups (decision record 0001).

The subscription groups and the API groups decide the route; the purpose never does, and
membership of the app is the caller check's gate, before any route is chosen. Test values:
subscription groups ``/old-sub`` and ``/new-sub``, API group ``/api-members`` and one Claude
profile; every run names the model the configuration chose for it.
"""

from __future__ import annotations

import pytest

from agent_runtime_service.config import IdentityConfig, ProfileConfig, Provider
from agent_runtime_service.routing import Api, Cli, NotEnabled, route

IDENTITY = IdentityConfig(
    issuer="https://keycloak.test/realms/agent-test",
    jwks_url="https://keycloak.test/realms/agent-test/protocol/openid-connect/certs",
    audience="agent-service",
    subscription_groups=frozenset({"/old-sub", "/new-sub"}),
    api_groups=frozenset({"/api-members"}),
    group_claim="groups",
)
PROFILES = {"claude-main": ProfileConfig(provider=Provider.CLAUDE)}
MODEL = f"{Provider.CLAUDE}/sonnet"
CLI = Cli("claude-main")
API = Api()
OFF = NotEnabled()


def _routed(groups, *, profiles=PROFILES, identity=IDENTITY, model=MODEL):
    return route(identity, profiles, frozenset(groups), model)


@pytest.mark.parametrize(
    ("groups", "profiles", "expected"),
    [
        pytest.param({"/new-sub"}, PROFILES, CLI, id="R1-new-subscription-group"),
        pytest.param({"/old-sub"}, PROFILES, CLI, id="R2-stale-token-keeps-its-route"),
        pytest.param({"/old-sub", "/new-sub"}, PROFILES, CLI, id="R3-both-names"),
        pytest.param({"/new-sub", "/api-members"}, PROFILES, CLI, id="R4-subscription-wins"),
        pytest.param({"/new-sub", "/api-members"}, {}, API, id="R5-no-profile-uses-the-api"),
        pytest.param({"/new-sub"}, {}, OFF, id="R6-nothing-configured"),
        pytest.param({"/api-members"}, PROFILES, API, id="R7-api-member"),
        pytest.param({"/new-sub/friends"}, PROFILES, OFF, id="R12-subscription-subgroup"),
        pytest.param({"/new-sub-x"}, PROFILES, OFF, id="R12-subscription-look-alike"),
        pytest.param({"/api-members/friends"}, PROFILES, OFF, id="R12a-api-subgroup"),
        pytest.param({"/api-members-x"}, PROFILES, OFF, id="R12a-api-look-alike"),
        pytest.param(set(), PROFILES, OFF, id="R13-no-groups"),
    ],
)
def test_the_route_follows_the_groups(groups, profiles, expected):
    assert _routed(groups, profiles=profiles) == expected


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
        pytest.param(SUBSCRIBER, "claude/sonnet", Cli("claude-main"), id="R-a-S"),
        pytest.param(API_ONLY, "claude/sonnet", Api(), id="R-a-A"),
        pytest.param(BOTH, "claude/sonnet", Cli("claude-main"), id="R-a-D"),
        pytest.param(SUBSCRIBER, "xai/sonnet", OFF, id="R-d-S"),
        pytest.param(BOTH, "xai/sonnet", Api(), id="R-d-D"),
        pytest.param(set(), "claude/sonnet", OFF, id="R-e-N"),
    ],
)
def test_a_named_model_runs_on_the_subscription_only_when_a_profile_has_its_provider(
    groups, model, expected
):
    assert _routed(groups, model=model) == expected


CLAUDE = ProfileConfig(provider=Provider.CLAUDE)
CODEX = ProfileConfig(provider=Provider.CODEX)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        pytest.param("claude/sonnet", Cli("claude-main"), id="claude-model"),
        pytest.param("codex/gpt-5.5", Cli("codex-main"), id="codex-model"),
    ],
)
def test_a_model_goes_to_the_profile_of_its_provider(model, expected):
    profiles = {"claude-main": CLAUDE, "codex-main": CODEX}

    assert _routed(SUBSCRIBER, profiles=profiles, model=model) == expected
