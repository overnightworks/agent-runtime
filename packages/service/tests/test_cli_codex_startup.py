"""The service starts on a Codex table: the startup rows only Codex has.

The rows every table has are those of ``test_cli_table_startup``.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.service_table_support import CODEX_TABLE, the_refusal_of

from agent_runtime_service.config import Provider


def test_a_grok_table_is_refused_as_an_unknown_key(tmp_path: Path) -> None:
    document = CODEX_TABLE.document(
        tmp_path, CODEX_TABLE.service_fake(tmp_path), CODEX_TABLE.login_file(tmp_path)
    )
    document["cli"]["grok"] = document["cli"][Provider.OPENAI]

    assert "cli.grok" in the_refusal_of(document)
