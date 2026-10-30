"""The login writer through the command for an xAI table: the documents its reader refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.support.provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    SAMPLE_CLAUDE_SETUP_TOKEN,
    a_codex_login,
)
from tests.support.service_login_support import (  # noqa: F401 - the fixtures are named in their decorators
    CANARY_FOREIGN,
    GROK_FILE_REFUSAL,
    Said,
    assert_document_refused,
    the_signal_handlers_restored,
    what_the_writer_returned,
    what_was_said,
)

from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_service.config import Provider
from agent_runtime_service.login_file import SwapResult

NOT_A_GROK_DOCUMENT = "is not a Grok login document"

REFUSED_XAI_DOCUMENTS = [
    pytest.param(b"", (), NOT_A_GROK_DOCUMENT, id="empty"),
    pytest.param(
        json.dumps({"realm": {"key": 5}}).encode(), (), NOT_A_GROK_DOCUMENT, id="key-not-a-string"
    ),
    pytest.param(
        b"not json " + CANARY_FOREIGN.encode(),
        (CANARY_FOREIGN,),
        NOT_A_GROK_DOCUMENT,
        id="not-json",
    ),
    pytest.param(
        json.dumps({"XAI_API_KEY": CANARY_FOREIGN}).encode(),
        (CANARY_FOREIGN,),
        GROK_FILE_REFUSAL,
        id="api-key-form",
    ),
    pytest.param(
        json.dumps({"realm": {"key": None, "refresh_token": CANARY_FOREIGN}}).encode(),
        (CANARY_FOREIGN,),
        GROK_FILE_REFUSAL,
        id="keyless-realm",
    ),
    pytest.param(
        a_codex_login(),
        (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN),
        GROK_FILE_REFUSAL,
        id="codex-document",
    ),
    pytest.param(
        SAMPLE_CLAUDE_SETUP_TOKEN.encode(),
        (SAMPLE_CLAUDE_SETUP_TOKEN,),
        NOT_A_GROK_DOCUMENT,
        id="claude-token",
    ),
    pytest.param(
        b"x" * (MAX_SECRET_FILE_BYTES + 1000),
        (),
        f"is larger than {MAX_SECRET_FILE_BYTES} bytes",
        id="too-large",
    ),
]


@pytest.mark.parametrize(("document", "canaries", "reason"), REFUSED_XAI_DOCUMENTS)
def test_a_document_the_xai_reader_refuses_is_not_written_and_the_old_file_stays(
    tmp_path: Path,
    said: Said,
    refusals: list[SwapResult],
    document: bytes,
    canaries: tuple[str, ...],
    reason: str,
) -> None:
    assert_document_refused(tmp_path, said, refusals, Provider.XAI, document, canaries, reason)
