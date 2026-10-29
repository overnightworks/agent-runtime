"""The one decider of whether a Codex login can start a child."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, NoReturn

_TOP_LEVEL_FIELDS: Final = ("auth_mode", "last_refresh")
_TOKEN_FIELDS: Final = ("access_token", "id_token", "account_id")
_FUTURE_ISSUE_SKEW_SECONDS: Final = 300
_LARGEST_CLAIM: Final = 2**53 - 1
_RENEWAL_LAST_REFRESH_AGE: Final = timedelta(days=30)
_LAST_REFRESH_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"
_BASE64URL_PAYLOAD: Final = re.compile(r"[A-Za-z0-9_-]*")
_TOKEN_SEGMENTS: Final = 3


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


class UnreadableTiming(StrEnum):
    NO_ACCESS_TOKEN = "the document holds no access token"
    NOT_A_TOKEN = "the access token's claims cannot be read"
    CLAIM_MISSING = "iat or exp is missing"
    WRONG_TYPE = "iat or exp is not a JSON integer"
    OUT_OF_RANGE = "iat or exp is outside 1 to 2**53 - 1"
    EXPIRY_NOT_AFTER_ISSUE = "exp is not after iat"
    ISSUED_IN_THE_FUTURE = "iat is more than 300 s after now"


@dataclass(frozen=True)
class CodexTiming:
    """When an access token was issued and when it expires, in whole epoch seconds."""

    issued_at: int
    expires_at: int

    def is_due(self, now: int) -> bool:
        """Whether half of the token's lifetime, rounded down, has passed."""
        return now >= self.issued_at + (self.expires_at - self.issued_at) // 2


def read_codex_timing(document: bytes, now: int) -> CodexTiming | UnreadableTiming:
    """The access token's issue and expiry times, or the one reason they cannot be used.

    The token's header and signature are not verified: the timing only schedules a renewal,
    and the token is never trusted as proof of anything. ``now`` is wall time in whole seconds.
    """
    access_token = _access_token(document)
    if access_token is None:
        return UnreadableTiming.NO_ACCESS_TOKEN
    claims = _claims(access_token)
    if claims is None:
        return UnreadableTiming.NOT_A_TOKEN
    return _timing_of(claims, now)


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


def _claims(access_token: str) -> dict[str, object] | None:
    segments = access_token.split(".")
    if len(segments) != _TOKEN_SEGMENTS or not _BASE64URL_PAYLOAD.fullmatch(segments[1]):
        return None
    try:
        padded = segments[1] + "=" * (-len(segments[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, RecursionError):
        return None
    return claims if isinstance(claims, dict) else None


def _timing_of(claims: dict[str, object], now: int) -> CodexTiming | UnreadableTiming:
    if "iat" not in claims or "exp" not in claims:
        return UnreadableTiming.CLAIM_MISSING
    issued_at, expires_at = claims["iat"], claims["exp"]
    if type(issued_at) is not int or type(expires_at) is not int:
        return UnreadableTiming.WRONG_TYPE
    if not all(1 <= claim <= _LARGEST_CLAIM for claim in (issued_at, expires_at)):
        return UnreadableTiming.OUT_OF_RANGE
    if expires_at <= issued_at:
        return UnreadableTiming.EXPIRY_NOT_AFTER_ISSUE
    if issued_at > now + _FUTURE_ISSUE_SKEW_SECONDS:
        return UnreadableTiming.ISSUED_IN_THE_FUTURE
    return CodexTiming(issued_at, expires_at)


class CodexRenewalRefusal(StrEnum):
    NOT_USABLE = "the document is not a usable Codex login"
    NO_REFRESH_TOKEN = "the login holds no refresh token"


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


def renewable_codex_login(document: bytes) -> RenewableCodexLogin | CodexRenewalRefusal:
    """The login when it is usable and holds a non-empty refresh token, else the refusal."""
    if assess_codex_login(document).verdict is not CodexLoginVerdict.USABLE:
        return CodexRenewalRefusal.NOT_USABLE
    if _refresh_token(document) is None:
        return CodexRenewalRefusal.NO_REFRESH_TOKEN
    return RenewableCodexLogin(document)


def made_due_copy(login: RenewableCodexLogin, now: int) -> bytes:
    """The login with an empty access token and a ``last_refresh`` 30 days before ``now``.

    A CLI given this copy finds its access token due and renews it with the refresh token.
    """
    document = json.loads(login.document)
    document["tokens"]["access_token"] = ""
    aged = datetime.fromtimestamp(now, UTC) - _RENEWAL_LAST_REFRESH_AGE
    document["last_refresh"] = aged.strftime(_LAST_REFRESH_FORMAT)
    return json.dumps(document).encode()
