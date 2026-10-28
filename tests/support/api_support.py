"""The gateway credential file and the ``[api]`` table every service test configuration carries."""

from __future__ import annotations

import atexit
import functools
import json
import shutil
import tempfile
from pathlib import Path
from typing import Final

API_COMPLETION_URL: Final = "http://gateway.internal/v1/chat/completions"


def write_api_credentials(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


@functools.cache
def process_api_credential_file() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="agent-runtime-service-api-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    credentials = {"shared_key": "synthetic-gateway-key"}
    return write_api_credentials(directory / "gateway.json", credentials)


API_SECTION: Final = (
    f"[api]\ncompletion_url = {json.dumps(API_COMPLETION_URL)}\n"
    f"credential_file = {json.dumps(str(process_api_credential_file()))}\n"
)
