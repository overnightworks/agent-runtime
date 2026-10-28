"""The throwaway Keycloak realm of `packages/service/tests/fixtures/keycloak/realm.json`.

Imports only httpx, webauth (inside `token_verifier`) and the standard library:
the `keycloak-token-shapes` CI job runs `test_identity_keycloak_token_shapes.py` with
`--noconftest` in an environment that has no workspace.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping

import httpx

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL")

REALM = "agent-test"
AGENT_SERVICE_AUDIENCE = "agent-service"
USER_LOGIN_CLIENT = "songmaker-test"

# Fixed test-only secrets of the throwaway realm in
# packages/service/tests/fixtures/keycloak/realm.json.
CLIENT_SECRETS = {
    "songmaker-test": "songmaker-test-secret",
    "presentator-test": "presentator-test-secret",
}
USER_PASSWORDS = {
    "alice": "alice-test-password",
    "bob": "bob-test-password",
    "carol": "carol-test-password",
    "dave": "dave-test-password",
    "erin": "erin-test-password",
    "frank": "frank-test-password",
}

Claims = Mapping[str, object]


def realm_url() -> str:
    return f"{KEYCLOAK_URL}/realms/{REALM}"


def fetch_key_set_from_test_keycloak(_configured_url: str) -> bytes:
    """The test Keycloak serves plain HTTP, while the verifier accepts only an
    https key-set URL; its injected fetch is the seam that reads the same key
    set from the realm's real certs endpoint instead."""
    response = httpx.get(f"{realm_url()}/protocol/openid-connect/certs", timeout=10)
    response.raise_for_status()
    return response.content


def token_verifier() -> Callable[[str], Claims]:
    from webauth.jwt_verification import JwtVerifier, VerifiedClaims

    verifier = JwtVerifier(
        issuer=realm_url(),
        audiences=[AGENT_SERVICE_AUDIENCE],
        jwks_url=f"https://keycloak.test/realms/{REALM}/protocol/openid-connect/certs",
        leeway_seconds=0,
        fetch_key_set=fetch_key_set_from_test_keycloak,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    )

    def verified_claims(token: str) -> Claims:
        result = verifier.verify(token)
        assert isinstance(result, VerifiedClaims), f"webauth rejected the token: {result}"
        return result.claims

    return verified_claims


def access_token(form: dict[str, str]) -> str:
    response = httpx.post(f"{realm_url()}/protocol/openid-connect/token", data=form, timeout=10)
    assert response.status_code == 200, (
        f"token endpoint answered {response.status_code}: {response.text}"
    )
    return response.json()["access_token"]
