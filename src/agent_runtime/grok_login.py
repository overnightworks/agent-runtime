"""The one decider of whether a Grok login can start a child."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, NoReturn

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

    The document is a JSON object whose every member is a realm object; a realm's
    ``key`` is absent or a string, and the login is usable once some realm has a
    non-empty one.
    """
    try:
        parsed = json.loads(document.decode("utf-8"))
        if not isinstance(parsed, dict) or not all(
            isinstance(realm, dict) for realm in parsed.values()
        ):
            return GrokLogin(GrokLoginVerdict.MALFORMED)
        keys = [realm.get("key") for realm in parsed.values()]
        if not all(key is None or isinstance(key, str) for key in keys):
            return GrokLogin(GrokLoginVerdict.MALFORMED)
        if not any(keys):
            return GrokLogin(GrokLoginVerdict.INCOMPLETE)
        return GrokLogin(GrokLoginVerdict.USABLE, json.dumps(_blanked(parsed)).encode())
    except (ValueError, RecursionError):
        return GrokLogin(GrokLoginVerdict.MALFORMED)


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
