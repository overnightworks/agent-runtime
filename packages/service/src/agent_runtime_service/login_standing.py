"""Where a provider's login stands for its renewal, judged from the document alone.

Pure: the document, the wall time and the provider's ``RenewalEntry`` are arguments, and the
verdict is a closed value that holds no token.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from agent_runtime.codex_login import CodexRenewalRefusal, UnreadableTiming
from agent_runtime_service.renewal import RenewalEntry
from agent_runtime_service.renewal_schedule import (
    RENEWAL_BACKOFF_SECONDS,
    RENEWAL_INTERVAL_SECONDS,
)

# A first attempt within one interval, a retry one backoff later, and one interval for the
# attempts' own time.
RENEWAL_WINDOW_SECONDS: Final = 2 * RENEWAL_INTERVAL_SECONDS + RENEWAL_BACKOFF_SECONDS


class LoginStanding(StrEnum):
    USABLE = "not due for renewal"
    DUE = "due for renewal"
    EXPIRY_UNKNOWN = "its expiry cannot be read"
    NOT_USABLE = "not a usable login"
    NOT_RENEWABLE = "holds no refresh token"
    EXPIRED = "expired"
    NOT_RENEWED_IN_TIME = "not renewed in time"

    @property
    def asks_for_sign_in(self) -> bool:
        return self in _ASKS_FOR_SIGN_IN


_ASKS_FOR_SIGN_IN: Final = frozenset(
    {
        LoginStanding.NOT_USABLE,
        LoginStanding.NOT_RENEWABLE,
        LoginStanding.EXPIRED,
        LoginStanding.NOT_RENEWED_IN_TIME,
    }
)


@dataclass(frozen=True)
class Standing:
    state: LoginStanding
    expires_at: int | None = None
    cause: UnreadableTiming | None = None


def login_standing(document: bytes, now: int, entry: RenewalEntry) -> Standing:
    """The standing of ``document`` at ``now``; never raises."""
    renewable = entry.renewable(document)
    if isinstance(renewable, CodexRenewalRefusal):
        refused = (
            LoginStanding.NOT_USABLE
            if renewable is CodexRenewalRefusal.NOT_USABLE
            else LoginStanding.NOT_RENEWABLE
        )
        return Standing(refused)
    timing = entry.timing(document, now)
    if isinstance(timing, UnreadableTiming):
        return Standing(LoginStanding.EXPIRY_UNKNOWN, cause=timing)
    if now >= timing.expires_at:
        return Standing(LoginStanding.EXPIRED, timing.expires_at)
    if timing.is_due(now - RENEWAL_WINDOW_SECONDS):
        return Standing(LoginStanding.NOT_RENEWED_IN_TIME, timing.expires_at)
    state = LoginStanding.DUE if timing.is_due(now) else LoginStanding.USABLE
    return Standing(state, timing.expires_at)
