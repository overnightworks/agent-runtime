"""The service's signer minting the capabilities that the client's MCP server verifies."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from packages.client.tests.support.capability_minting import LIFETIME
from packages.client.tests.support.mcp_server_support import (
    ALLOWED_TOOLS,
    ISSUED_AT,
    PRIVATE_KEY,
    RUN_ID,
)

from agent_runtime_service.capability import (
    Capability,
    CapabilityScope,
    capability_claims,
    signed_capability,
)
from agent_runtime_service.signing import SigningKey

SIGNING_KEY = SigningKey.from_private_key(PRIVATE_KEY)


def minted_capability(
    origin: str,
    *,
    run_id: UUID = RUN_ID,
    tools: Sequence[str] = ALLOWED_TOOLS,
    issued_at: float = ISSUED_AT,
) -> Capability:
    scope = CapabilityScope(origin, tuple(tools), SIGNING_KEY)
    claims = capability_claims(
        scope,
        run_id=run_id,
        app="notes",
        issuer="https://keycloak.test/realms/agent-test",
        subject="user-alice",
        agent="notes.assistant",
        now=issued_at,
        max_duration_s=LIFETIME,
    )
    return signed_capability(scope, claims)
