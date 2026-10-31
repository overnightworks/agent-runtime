"""The one reader of when a login's access token was issued and when it expires."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_FUTURE_ISSUE_SKEW_SECONDS: Final = 300
_LARGEST_CLAIM: Final = 2**53 - 1
_BASE64URL_PAYLOAD: Final = re.compile(r"[A-Za-z0-9_-]*")
_TOKEN_SEGMENTS: Final = 3


class UnreadableTiming(StrEnum):
    NO_ACCESS_TOKEN = "the document holds no access token"  # noqa: S105 - closed cause text names no secret
    SEVERAL_ACCESS_TOKENS = "the document holds more than one access token"  # noqa: S105 - closed cause text names no secret
    NOT_A_TOKEN = "the access token's claims cannot be read"  # noqa: S105 - closed cause text names no secret
    CLAIM_MISSING = "iat or exp is missing"
    WRONG_TYPE = "iat or exp is not a JSON integer"
    OUT_OF_RANGE = "iat or exp is outside 1 to 2**53 - 1"
    EXPIRY_NOT_AFTER_ISSUE = "exp is not after iat"
    ISSUED_IN_THE_FUTURE = "iat is more than 300 s after now"


@dataclass(frozen=True)
class AccessTokenTiming:
    """When an access token was issued and when it expires, in whole epoch seconds."""

    issued_at: int
    expires_at: int

    def is_due(self, now: int) -> bool:
        """Whether half of the token's lifetime, rounded down, has passed."""
        return now >= self.issued_at + (self.expires_at - self.issued_at) // 2


def read_access_token_timing(access_token: str, now: int) -> AccessTokenTiming | UnreadableTiming:
    """The access token's issue and expiry times, or the one reason they cannot be used.

    The token's header and signature are not verified: the timing only schedules a renewal,
    and the token is never trusted as proof of anything. ``now`` is wall time in whole seconds.
    """
    claims = _claims(access_token)
    if claims is None:
        return UnreadableTiming.NOT_A_TOKEN
    return _timing_of(claims, now)


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


def _timing_of(claims: dict[str, object], now: int) -> AccessTokenTiming | UnreadableTiming:
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
    return AccessTokenTiming(issued_at, expires_at)
