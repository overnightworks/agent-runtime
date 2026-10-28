"""The token shapes a real Keycloak 26.8.1 issues for the agent service's callers.

The AI entrance builds on these claims, so they are proven against a real
issuer rather than assumed: `packages/service/tests/fixtures/keycloak/realm.json` mirrors the
platform realm, the `keycloak-token-shapes` CI job serves it from Keycloak, and every
token here comes from the realm's token endpoint and is verified against its
published key set with webauth's RS256 verifier.

Runs only where `KEYCLOAK_URL` names that Keycloak. Needs nothing from the
workspace: the job runs it with `--noconftest` in the hash-locked environment
of `packages/service/tests/fixtures/keycloak/requirements.txt`.
"""

from __future__ import annotations

import pytest
from tests.support.keycloak_realm import (
    AGENT_SERVICE_AUDIENCE,
    CLIENT_SECRETS,
    KEYCLOAK_URL,
    USER_LOGIN_CLIENT,
    USER_PASSWORDS,
    Claims,
    access_token,
    token_verifier,
)

pytestmark = [
    pytest.mark.keycloak,
    pytest.mark.skipif(KEYCLOAK_URL is None, reason="KEYCLOAK_URL names no test Keycloak"),
]

WORKLOAD_ROLE = "agent-workload"
MAX_ACCESS_TOKEN_SECONDS = 300

EXPECTED_GROUPS = {
    "alice": {"/subscription-members", "/apps/songmaker"},
    "bob": {"/api-members", "/apps/songmaker"},
    "carol": set(),
    "dave": {"/subscription-members", "/apps/songmaker/admin"},
    "erin": {"/subscription-members", "/apps/songmaker-other"},
    "frank": {"/subscription-members", "/apps/presentator"},
}

WORKLOAD_CLIENTS = sorted(CLIENT_SECRETS)
USERS = sorted(USER_PASSWORDS)


@pytest.fixture(scope="module")
def verify():
    return token_verifier()


@pytest.fixture(scope="module")
def service_account_claims(verify) -> dict[str, Claims]:
    return {
        client: verify(
            access_token(
                {
                    "grant_type": "client_credentials",
                    "client_id": client,
                    "client_secret": secret,
                }
            )
        )
        for client, secret in CLIENT_SECRETS.items()
    }


@pytest.fixture(scope="module")
def user_claims(verify) -> dict[str, Claims]:
    return {
        user: verify(
            access_token(
                {
                    "grant_type": "password",
                    "client_id": USER_LOGIN_CLIENT,
                    "client_secret": CLIENT_SECRETS[USER_LOGIN_CLIENT],
                    "username": user,
                    "password": password,
                }
            )
        )
        for user, password in USER_PASSWORDS.items()
    }


def _realm_roles(claims: Claims) -> set[str]:
    realm_access = claims.get("realm_access") or {}
    return set(realm_access.get("roles", []))


@pytest.mark.parametrize("client", WORKLOAD_CLIENTS)
def test_service_account_token_names_its_client_in_azp_and_client_id(
    service_account_claims, client
):
    claims = service_account_claims[client]
    assert claims.get("azp") == client, f"azp is {claims.get('azp')!r}, not {client!r}"
    assert claims.get("client_id") == client, (
        f"client_id is {claims.get('client_id')!r}, not {client!r}"
    )


@pytest.mark.parametrize("client", WORKLOAD_CLIENTS)
def test_service_account_token_carries_the_agent_workload_role(service_account_claims, client):
    roles = _realm_roles(service_account_claims[client])
    assert WORKLOAD_ROLE in roles, f"realm_access.roles {sorted(roles)} lacks {WORKLOAD_ROLE}"


@pytest.mark.parametrize("client", WORKLOAD_CLIENTS)
def test_service_account_token_lives_at_most_300_seconds(service_account_claims, client):
    claims = service_account_claims[client]
    lifetime = claims["exp"] - claims["iat"]
    assert lifetime <= MAX_ACCESS_TOKEN_SECONDS, f"exp - iat is {lifetime} s, over 300 s"


@pytest.mark.parametrize("user", USERS)
def test_user_token_never_carries_the_agent_workload_role(user_claims, user):
    roles = _realm_roles(user_claims[user])
    assert WORKLOAD_ROLE not in roles, (
        f"realm_access.roles of {user} carries {WORKLOAD_ROLE}: {sorted(roles)}"
    )


@pytest.mark.parametrize("user", USERS)
def test_user_token_names_the_login_client_in_azp(user_claims, user):
    claims = user_claims[user]
    # Recorded, not relied on: whether a user token carries client_id.
    print(f"{user}: client_id {claims.get('client_id', '<absent>')!r}")
    assert claims.get("azp") == USER_LOGIN_CLIENT, (
        f"azp of {user} is {claims.get('azp')!r}, not {USER_LOGIN_CLIENT!r}"
    )


@pytest.mark.parametrize("user", USERS)
def test_user_token_audience_contains_the_agent_service(user_claims, user):
    audience = user_claims[user].get("aud")
    audiences = {audience} if isinstance(audience, str) else set(audience or [])
    assert AGENT_SERVICE_AUDIENCE in audiences, (
        f"aud of {user} is {audience!r}, without {AGENT_SERVICE_AUDIENCE}"
    )


@pytest.mark.parametrize("user", USERS)
def test_user_token_lists_groups_as_full_paths(user_claims, user):
    groups = user_claims[user].get("groups") or []
    assert set(groups) == EXPECTED_GROUPS[user], (
        f"groups of {user} are {groups!r}, not {sorted(EXPECTED_GROUPS[user])}"
    )


@pytest.mark.parametrize("user", USERS)
def test_user_token_subject_differs_from_every_service_account_subject(
    user_claims, service_account_claims, user
):
    subject = user_claims[user]["sub"]
    service_account_subjects = {claims["sub"] for claims in service_account_claims.values()}
    assert subject not in service_account_subjects, (
        f"sub of {user} equals a service account's sub {subject!r}"
    )


# These belong to the single token, not to the client that asked for it.
PER_TOKEN_CLAIMS = {"exp", "iat", "jti"}
CLIENT_NAMING_CLAIMS = {"azp", "client_id"}
# The service-account user Keycloak generated for each client: its id and its
# username, which no registry knows beforehand.
SERVICE_ACCOUNT_USER_CLAIMS = {"sub", "preferred_username"}
CLIENT_WEB_ORIGIN_CLAIMS = {"allowed-origins"}
# songmaker's service account joins a group and presentator's does not.
SERVICE_ACCOUNT_GROUP_CLAIMS = {"groups"}


def test_service_account_tokens_differ_in_the_client_its_user_and_its_web_origins(
    service_account_claims,
):
    songmaker = service_account_claims["songmaker-test"]
    presentator = service_account_claims["presentator-test"]
    differing = {
        claim
        for claim in (songmaker.keys() | presentator.keys()) - PER_TOKEN_CLAIMS
        if songmaker.get(claim) != presentator.get(claim)
    }
    print(f"service-account claims: {sorted(songmaker.keys() | presentator.keys())}")
    print(f"differing between the two workload clients: {sorted(differing)}")
    expected = (
        CLIENT_NAMING_CLAIMS
        | SERVICE_ACCOUNT_USER_CLAIMS
        | CLIENT_WEB_ORIGIN_CLAIMS
        | SERVICE_ACCOUNT_GROUP_CLAIMS
    )
    assert differing == expected, (
        f"service-account tokens differ in {sorted(differing)}, not {sorted(expected)}"
    )


def test_service_account_token_lists_its_groups_as_full_paths_and_may_omit_an_empty_claim(
    service_account_claims,
):
    songmaker = service_account_claims["songmaker-test"]
    presentator = service_account_claims["presentator-test"]
    print(f"presentator's token carries a groups claim: {'groups' in presentator}")
    assert songmaker.get("groups") == ["/api-members"], (
        f"groups of songmaker's service account are {songmaker.get('groups')!r}"
    )
    assert presentator.get("groups", []) == [], (
        f"groups of presentator's service account are {presentator.get('groups')!r}"
    )
