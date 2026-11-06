"""A run capability minted with the client's own claim model, with no service involved."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_runtime_client.capability import (
    CAPABILITY_ISSUER,
    CAPABILITY_TOKEN_TYPE,
    CapabilityClaims,
    CapabilityPrincipal,
)

LIFETIME = 600
KEY_ID = "key-1"


@dataclass(frozen=True)
class MintedCapability:
    claims: CapabilityClaims
    token: str = field(repr=False)


def minted_claims(
    origin: str, tools: Sequence[str], *, run_id: UUID, now: float
) -> CapabilityClaims:
    return CapabilityClaims(
        iss=CAPABILITY_ISSUER,
        aud=origin,
        sub=str(run_id),
        iat=math.floor(now),
        exp=math.floor(now + LIFETIME),
        app="notes",
        agent="notes.assistant",
        principal=CapabilityPrincipal(
            iss="https://keycloak.test/realms/agent-test", sub="user-alice"
        ),
        tools=tuple(tools),
    )


def signed_capability(claims: CapabilityClaims, private_key: rsa.RSAPrivateKey) -> MintedCapability:
    token = jwt.encode(
        claims.model_dump(mode="json"),
        private_key,
        algorithm="RS256",
        headers={"kid": KEY_ID, "typ": CAPABILITY_TOKEN_TYPE},
    )
    return MintedCapability(claims, token)
