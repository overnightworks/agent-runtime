"""Runs from the three installed wheels alone: the service's upstream client discovers a tool.

Started as ``python -I <this file>``, which leaves this directory off
``sys.path``; the fake app lives in ``tests/support``, so the repository root is added.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[4])
)  # the fake app is test code, not a wheel

from tests.support.capability_support import capability_carrying  # noqa: E402
from tests.support.mcp_app_fake import CERTIFICATE_HOST, ScriptedApp, TestAuthority, serving  # noqa: E402

from agent_runtime_service.app_tools import open_app_tools  # noqa: E402

DISCOVERY_DEADLINE_SECONDS = 30


async def discover(origin: str, authority: TestAuthority) -> list[str]:
    async with asyncio.timeout(DISCOVERY_DEADLINE_SECONDS):
        async with open_app_tools(
            origin, capability_carrying("smoke-capability"), authority.client_context()
        ) as session:
            return [tool.name for tool in await session.list_tools()]


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        authority = TestAuthority(Path(directory))
        with serving(ScriptedApp(), authority.credentials_for(CERTIFICATE_HOST)) as fake:
            discovered = asyncio.run(discover(fake.origin, authority))
    assert discovered == ["echo"], discovered


main()
