"""The one decider of whether a Grok login can start a child."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, NoReturn

from agent_runtime.login_renewal import RenewalRefusal
from agent_runtime.login_timing import AccessTokenTiming, UnreadableTiming, read_access_token_timing

_RENEWAL_NAMES: Final = frozenset({"refresh_token", "refreshToken", "refresh"})


class GrokLoginVerdict(StrEnum):
    USABLE = "usable"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class GrokLogin:
    """The verdict on a login document and, when usable, the copy a child may hold.

    The blanked document keeps the access key, so it refuses to be shown or
    serialised.
    """

    verdict: GrokLoginVerdict
    blanked_document: bytes | None = field(default=None, repr=False)

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Grok login holds credentials and cannot be serialised")


def assess_grok_login(document: bytes) -> GrokLogin:
    """Assess a login document: usable, without a key yet, or unreadable.

    The document is a JSON object. Its members that are objects are realms; any
    other member is ignored, because the vendor may add members to its own file.
    A realm's ``key`` is absent, null (counts as absent) or a string, and the
    login is usable once some realm has a non-empty one.
    """
    parsed = _parsed_login(document)
    if parsed is None:
        return GrokLogin(GrokLoginVerdict.MALFORMED)
    keys = _realm_keys(parsed)
    if not all(key is None or isinstance(key, str) for key in keys):
        return GrokLogin(GrokLoginVerdict.MALFORMED)
    if not any(keys):
        return GrokLogin(GrokLoginVerdict.INCOMPLETE)
    try:
        copy = json.dumps(_blanked(parsed)).encode()
    except RecursionError:
        return GrokLogin(GrokLoginVerdict.MALFORMED)
    return GrokLogin(GrokLoginVerdict.USABLE, copy)


def blanked_copy(document: bytes) -> bytes | None:
    """Return the login a child may hold: the document with its renewal members empty.

    A child that renews would rotate the operator's real refresh token and break
    the stored login, so every Grok child receives this copy instead of the
    operator's file. A document without a usable realm has no copy.
    """
    return assess_grok_login(document).blanked_document


def _blanked(value: object) -> object:
    if isinstance(value, dict):
        return {
            name: "" if name in _RENEWAL_NAMES else _blanked(member)
            for name, member in value.items()
        }
    if isinstance(value, list):
        return [_blanked(member) for member in value]
    return value


def _parsed_login(document: bytes) -> dict[str, object] | None:
    try:
        parsed = json.loads(document.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _realm_keys(parsed: dict[str, object]) -> list[object]:
    """The ``key`` of every realm: the object members of the document."""
    return [member.get("key") for member in parsed.values() if isinstance(member, dict)]


def _renewal_members(value: object) -> list[str]:
    """Every non-empty renewal member at any depth, in no fixed order."""
    if isinstance(value, dict):
        members = [
            member
            for name, member in value.items()
            if name in _RENEWAL_NAMES and isinstance(member, str) and member
        ]
        return members + [found for member in value.values() for found in _renewal_members(member)]
    if isinstance(value, list):
        return [found for member in value for found in _renewal_members(member)]
    return []


@dataclass(frozen=True)
class RenewableGrokLogin:
    """A usable login that still holds its refresh token; built only by ``renewable_grok_login``.

    It holds every token, so it refuses to be shown or serialised.
    """

    _document: bytes = field(repr=False)

    @property
    def document(self) -> bytes:
        return self._document

    def shares_refresh_token_with(self, other: RenewableGrokLogin) -> bool:
        """Whether both hold the same non-empty renewal members."""
        members = sorted(_renewal_members(_parsed_login(self._document)))
        return bool(members) and members == sorted(_renewal_members(_parsed_login(other.document)))

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Grok login holds credentials and cannot be serialised")


def renewable_grok_login(document: bytes) -> RenewableGrokLogin | RenewalRefusal:
    """The login when it is usable and holds a non-empty renewal member, else the refusal."""
    if assess_grok_login(document).verdict is not GrokLoginVerdict.USABLE:
        return RenewalRefusal.NOT_USABLE
    if not _renewal_members(_parsed_login(document)):
        return RenewalRefusal.NO_REFRESH_TOKEN
    return RenewableGrokLogin(document)


def read_grok_timing(document: bytes, now: int) -> AccessTokenTiming | UnreadableTiming:
    """The timing of the one realm's access key, or the one reason it cannot be used.

    ``expires_at`` is not read: its unit is not known, so only the key's own claims count.
    """
    realms = [
        key for key in _realm_keys(_parsed_login(document) or {}) if isinstance(key, str) and key
    ]
    if not realms:
        return UnreadableTiming.NO_ACCESS_TOKEN
    if len(realms) > 1:
        return UnreadableTiming.SEVERAL_ACCESS_TOKENS
    return read_access_token_timing(realms[0], now)
