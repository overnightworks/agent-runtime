"""Safe gateway credential documents and selection by authenticated app."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from pydantic import SecretStr

from agent_runtime.errors import raise_without_chain
from agent_runtime.secret_file import UnsoundSecretFile, read_secret_file


class InvalidApiCredentials(ValueError):
    """A missing or invalid gateway credential, without file content or parser chains."""


@dataclass(frozen=True)
class ApiCredentials:
    shared_key: SecretStr | None = field(repr=False)
    app_keys: Mapping[str, SecretStr] = field(repr=False)

    def for_app(self, app: str) -> SecretStr:
        key = self.app_keys.get(app, self.shared_key)
        if key is None:
            raise_without_chain(InvalidApiCredentials(
                "No API credential is configured for this app."))
        return key


def read_api_credentials(path: Path, apps: frozenset[str]) -> ApiCredentials:
    """Read and validate anew; only a closed refusal escapes parsing or file checks."""
    try:
        credentials = _parsed_credentials(read_secret_file(path, "API credential file"), apps)
    except UnsoundSecretFile:
        credentials = None
    if credentials is None:
        raise_without_chain(InvalidApiCredentials("API credential file is missing or invalid."))
    return credentials


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for name, value in pairs:
        if name in document:
            raise ValueError("Duplicate credential field.")
        document[name] = value
    return document


def _credential(value: object) -> SecretStr:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value) is None:
        raise ValueError("Invalid credential.")
    return SecretStr(value)


def _parsed_credentials(contents: bytes, apps: frozenset[str]) -> ApiCredentials | None:
    try:
        document = json.loads(contents.decode("utf-8"), object_pairs_hook=_unique_fields)
        if not isinstance(document, dict) or document.keys() - {"shared_key", "app_keys"}:
            return None
        shared = _credential(document["shared_key"]) if "shared_key" in document else None
        entries = document.get("app_keys", {})
        if not isinstance(entries, dict) or entries.keys() - apps:
            return None
        keys = {app: _credential(value) for app, value in entries.items()}
        if shared is None and not keys:
            return None
        return ApiCredentials(shared, MappingProxyType(keys))
    except (ValueError, RecursionError):
        return None
