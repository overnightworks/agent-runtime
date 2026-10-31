"""The one decider of whether a Codex login can start a child."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, NoReturn

from agent_runtime.login_renewal import RenewableLogin, RenewalRefusal
from agent_runtime.login_timing import AccessTokenTiming, UnreadableTiming, read_access_token_timing

_TOP_LEVEL_FIELDS: Final = ("auth_mode", "last_refresh")
_TOKEN_FIELDS: Final = ("access_token", "id_token", "account_id")
_RENEWAL_LAST_REFRESH_AGE: Final = timedelta(days=30)
_LAST_REFRESH_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"


class CodexLoginVerdict(StrEnum):
    USABLE = "usable"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class CodexLogin:
    """The verdict on a login document and, when usable, the copy a child may hold.

    The blanked document keeps the access and id tokens, so it refuses to be
    shown or serialised.
    """

    verdict: CodexLoginVerdict
    blanked_document: bytes | None = field(default=None, repr=False)

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Codex login holds credentials and cannot be serialised")


def assess_codex_login(document: bytes) -> CodexLogin:
    """Assess a login document: complete and renewable-free, unfinished, or unreadable."""
    try:
        parsed = json.loads(document)
    except (ValueError, RecursionError):
        return CodexLogin(CodexLoginVerdict.MALFORMED)
    if not isinstance(parsed, dict):
        return CodexLogin(CodexLoginVerdict.MALFORMED)
    if "tokens" not in parsed:
        return CodexLogin(CodexLoginVerdict.INCOMPLETE)
    tokens = parsed["tokens"]
    if not isinstance(tokens, dict):
        return CodexLogin(CodexLoginVerdict.MALFORMED)
    fields = [parsed.get(name, "") for name in _TOP_LEVEL_FIELDS]
    fields += [tokens.get(name, "") for name in _TOKEN_FIELDS]
    if not all(isinstance(value, str) for value in fields):
        return CodexLogin(CodexLoginVerdict.MALFORMED)
    if not all(fields):
        return CodexLogin(CodexLoginVerdict.INCOMPLETE)
    return CodexLogin(CodexLoginVerdict.USABLE, _blanked_document(parsed, tokens))


def blanked_copy(document: bytes) -> bytes | None:
    """Return the login a child may hold: complete, but not renewable, or ``None``.

    A child that refreshes would rotate the operator's real refresh token and
    break the stored login, so every Codex child receives this copy instead of
    the operator's file.
    """
    return assess_codex_login(document).blanked_document


def _blanked_document(document: dict[str, object], tokens: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "auth_mode": document["auth_mode"],
            "OPENAI_API_KEY": None,
            "last_refresh": document["last_refresh"],
            "tokens": {
                "id_token": tokens["id_token"],
                "access_token": tokens["access_token"],
                "account_id": tokens["account_id"],
                "refresh_token": "",
            },
        }
    ).encode()


def read_codex_timing(document: bytes, now: int) -> AccessTokenTiming | UnreadableTiming:
    """The Codex login's access-token timing, or the one reason it cannot be used."""
    access_token = _access_token(document)
    if access_token is None:
        return UnreadableTiming.NO_ACCESS_TOKEN
    return read_access_token_timing(access_token, now)


def _parsed_login(document: bytes) -> dict[str, object] | None:
    try:
        parsed = json.loads(document)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _tokens(document: bytes) -> dict[str, object] | None:
    tokens = (_parsed_login(document) or {}).get("tokens")
    return tokens if isinstance(tokens, dict) else None


def _access_token(document: bytes) -> str | None:
    access_token = (_tokens(document) or {}).get("access_token")
    return access_token if isinstance(access_token, str) and access_token else None


@dataclass(frozen=True)
class RenewableCodexLogin:
    """A usable login that still holds its refresh token; built only by ``renewable_codex_login``.

    It holds every token, so it refuses to be shown or serialised.
    """

    document: bytes = field(repr=False)

    def shares_refresh_token_with(self, other: RenewableCodexLogin) -> bool:
        return _refresh_token(self.document) == _refresh_token(other.document)

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Codex login holds credentials and cannot be serialised")


def _refresh_token(document: bytes) -> str | None:
    refresh_token = (_tokens(document) or {}).get("refresh_token")
    return refresh_token if isinstance(refresh_token, str) and refresh_token else None


def renewable_codex_login(document: bytes) -> RenewableCodexLogin | RenewalRefusal:
    """The login when it is usable and holds a non-empty refresh token, else the refusal."""
    if assess_codex_login(document).verdict is not CodexLoginVerdict.USABLE:
        return RenewalRefusal.NOT_USABLE
    if _refresh_token(document) is None:
        return RenewalRefusal.NO_REFRESH_TOKEN
    return RenewableCodexLogin(document)


def made_due_copy(login: RenewableLogin, now: int) -> bytes:
    """The login with an empty access token and a ``last_refresh`` 30 days before ``now``.

    A CLI given this copy finds its access token due and renews it with the refresh token.
    """
    document = json.loads(login.document)
    document["tokens"]["access_token"] = ""
    aged = datetime.fromtimestamp(now, UTC) - _RENEWAL_LAST_REFRESH_AGE
    document["last_refresh"] = aged.strftime(_LAST_REFRESH_FORMAT)
    return json.dumps(document).encode()
