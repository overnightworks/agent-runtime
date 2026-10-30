"""The one decider of whether a Codex login can start a child."""

from __future__ import annotations

import copy
import json
import pickle

import pytest
from tests.support.codex_support import a_codex_access_token
from tests.support.provider_test_support import (
    A_CODEX_LOGIN_WITH_INVALID_UTF8,
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    a_codex_login,
)

from agent_runtime.codex_login import (
    CodexLogin,
    CodexLoginVerdict,
    CodexRenewalRefusal,
    RenewableCodexLogin,
    assess_codex_login,
    made_due_copy,
    read_codex_timing,
    renewable_codex_login,
)
from agent_runtime.login_timing import UnreadableTiming

A_DEEPLY_NESTED_DOCUMENT = b"[" * 100_000
A_DOCUMENT_WITH_A_HUGE_INTEGER = b'{"tokens": {"account_id": ' + b"9" * 5_000 + b"}}"

USABLE, INCOMPLETE, MALFORMED = (
    CodexLoginVerdict.USABLE,
    CodexLoginVerdict.INCOMPLETE,
    CodexLoginVerdict.MALFORMED,
)


@pytest.mark.parametrize(
    ("document", "verdict"),
    [
        pytest.param(a_codex_login(), USABLE, id="complete"),
        pytest.param(A_CODEX_LOGIN_WITH_INVALID_UTF8, MALFORMED, id="invalid-encoding"),
        pytest.param(A_DEEPLY_NESTED_DOCUMENT, MALFORMED, id="excessive-nesting"),
        pytest.param(A_DOCUMENT_WITH_A_HUGE_INTEGER, MALFORMED, id="integer-over-the-digit-limit"),
        pytest.param(b"not json", MALFORMED, id="bad-syntax"),
        pytest.param(b"[]", MALFORMED, id="top-level-not-an-object"),
        pytest.param(
            b'{"auth_mode":"apikey","OPENAI_API_KEY":"sk-synthetic"}',
            INCOMPLETE,
            id="api-key-only",
        ),
        pytest.param(b'{"tokens":null}', MALFORMED, id="tokens-null"),
        pytest.param(b'{"tokens":[]}', MALFORMED, id="tokens-not-an-object"),
        pytest.param(b'{"auth_mode":"chatgpt","tokens":{}}', INCOMPLETE, id="empty-tokens"),
        pytest.param(a_codex_login(tokens={"id_token": ""}), INCOMPLETE, id="empty-id-token"),
        pytest.param(a_codex_login(auth_mode=""), INCOMPLETE, id="empty-auth-mode"),
        pytest.param(a_codex_login(last_refresh=None), MALFORMED, id="null-last-refresh"),
        pytest.param(a_codex_login(tokens={"account_id": 7}), MALFORMED, id="numeric-account-id"),
        pytest.param(b'{"tokens":{"account_id":null}}', MALFORMED, id="null-account-id"),
    ],
)
def test_assess_codex_login_names_each_document_shape(
    document: bytes,
    verdict: CodexLoginVerdict,
) -> None:
    assert assess_codex_login(document).verdict is verdict


def test_a_usable_login_hands_a_child_the_login_without_its_refresh_token() -> None:
    blanked = assess_codex_login(a_codex_login()).blanked_document

    assert (
        blanked
        == json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "last_refresh": "2026-09-04T19:20:00Z",
                "tokens": {
                    "id_token": CANARY_ID_TOKEN,
                    "access_token": CANARY_ACCESS_TOKEN,
                    "account_id": "account",
                    "refresh_token": "",
                },
            }
        ).encode()
    )


@pytest.mark.parametrize("document", [b"not json", b'{"tokens":{}}', a_codex_login(last_refresh=3)])
def test_a_login_that_is_not_usable_carries_no_document(document: bytes) -> None:
    assert assess_codex_login(document).blanked_document is None


@pytest.mark.parametrize(
    "disclose",
    [
        pytest.param(repr, id="repr"),
        pytest.param(str, id="str"),
        pytest.param(pickle.dumps, id="pickle"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
    ],
)
def test_a_usable_codex_login_never_shows_its_tokens(disclose) -> None:
    login = assess_codex_login(a_codex_login())
    assert isinstance(login, CodexLogin)

    if disclose in (repr, str):
        shown = disclose(login)
        assert CANARY_ACCESS_TOKEN not in shown
        assert CANARY_ID_TOKEN not in shown
    else:
        with pytest.raises(TypeError):
            disclose(login)


NOW = 1_800_000_000
READABLE_LIFETIME = {"iat": NOW - 100, "exp": NOW + 100}


def a_login_with_claims(claims: dict[str, object] | bytes) -> bytes:
    return a_codex_login(tokens={"access_token": a_codex_access_token(claims)})


def a_login_with_access_token(access_token: object) -> bytes:
    return a_codex_login(tokens={"access_token": access_token})


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b'{"tokens":{"access_token":"x"}}\xff', id="invalid-utf8"),
        pytest.param(b"[" * 100_000, id="nested"),
        pytest.param(b"[]", id="array"),
        pytest.param(b'{"tokens":[]}', id="tokens-not-an-object"),
        pytest.param(b'{"tokens":{}}', id="no-access-token"),
        pytest.param(a_login_with_access_token(""), id="empty-access-token"),
        pytest.param(a_login_with_access_token(7), id="numeric-access-token"),
    ],
)
def test_a_document_without_an_access_token_is_named_and_never_raises(document: bytes) -> None:
    assert read_codex_timing(document, NOW) is UnreadableTiming.NO_ACCESS_TOKEN


def test_an_opaque_access_token_still_gives_a_usable_login_but_no_timing() -> None:
    document = a_codex_login()

    assert assess_codex_login(document).verdict is USABLE
    assert read_codex_timing(document, NOW) is UnreadableTiming.NOT_A_TOKEN


@pytest.mark.parametrize(
    ("document", "refusal"),
    [
        pytest.param(
            a_codex_login(tokens={"refresh_token": ""}),
            CodexRenewalRefusal.NO_REFRESH_TOKEN,
            id="empty",
        ),
        pytest.param(
            a_codex_login(tokens={"refresh_token": 7}),
            CodexRenewalRefusal.NO_REFRESH_TOKEN,
            id="numeric",
        ),
        pytest.param(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "last_refresh": "x",
                    "tokens": {"access_token": "a", "id_token": "i", "account_id": "c"},
                }
            ).encode(),
            CodexRenewalRefusal.NO_REFRESH_TOKEN,
            id="missing",
        ),
        pytest.param(b"not json", CodexRenewalRefusal.NOT_USABLE, id="malformed"),
        pytest.param(b'{"tokens":{}}', CodexRenewalRefusal.NOT_USABLE, id="incomplete"),
    ],
)
def test_a_document_without_a_refresh_token_or_not_usable_is_refused(
    document: bytes, refusal: CodexRenewalRefusal
) -> None:
    assert renewable_codex_login(document) is refusal


def test_a_full_login_is_renewable_and_keeps_its_document() -> None:
    document = a_codex_login()

    login = renewable_codex_login(document)

    assert isinstance(login, RenewableCodexLogin)
    assert login.document == document


def test_logins_share_a_refresh_token_until_it_rotates() -> None:
    login = renewable_codex_login(a_codex_login())
    same_token_new_access = renewable_codex_login(a_codex_login(tokens={"access_token": "other"}))
    rotated = renewable_codex_login(a_codex_login(tokens={"refresh_token": "rotated"}))
    assert isinstance(login, RenewableCodexLogin)
    assert isinstance(same_token_new_access, RenewableCodexLogin)
    assert isinstance(rotated, RenewableCodexLogin)

    assert login.shares_refresh_token_with(same_token_new_access)
    assert not login.shares_refresh_token_with(rotated)


def a_renewable_login(**tokens: object) -> RenewableCodexLogin:
    document = a_codex_login(tokens=tokens, OPENAI_API_KEY="sk-extra", extra={"k": 1})
    login = renewable_codex_login(document)
    assert isinstance(login, RenewableCodexLogin)
    return login


def test_the_made_due_copy_empties_the_access_token_and_ages_last_refresh_by_30_days() -> None:
    copy_document = json.loads(made_due_copy(a_renewable_login(), NOW))

    assert copy_document["tokens"]["access_token"] == ""
    assert copy_document["last_refresh"] == "2026-12-16T08:00:00.000000Z"


def test_the_made_due_copy_changes_nothing_else_and_keeps_member_order() -> None:
    login = a_renewable_login()
    original = json.loads(login.document)
    changed = json.loads(made_due_copy(login, NOW))

    original["tokens"]["access_token"] = ""
    original["last_refresh"] = changed["last_refresh"]
    assert changed == original
    assert list(changed) == list(original)
    assert list(changed["tokens"]) == list(original["tokens"])
    assert changed["tokens"]["refresh_token"] == "canary-refresh-token"
    assert changed["OPENAI_API_KEY"] == "sk-extra"
    assert changed["extra"] == {"k": 1}


def test_the_made_due_copy_has_no_timing_and_leaves_the_login_document_alone() -> None:
    document = a_login_with_claims(READABLE_LIFETIME)
    login = renewable_codex_login(document)
    assert isinstance(login, RenewableCodexLogin)

    copied = made_due_copy(login, NOW)

    assert read_codex_timing(copied, NOW) is UnreadableTiming.NO_ACCESS_TOKEN
    assert login.document == document


@pytest.mark.parametrize(
    "disclose",
    [
        pytest.param(repr, id="repr"),
        pytest.param(str, id="str"),
        pytest.param(pickle.dumps, id="pickle"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
    ],
)
def test_a_renewable_login_never_shows_or_serialises_its_tokens(disclose) -> None:
    login = a_renewable_login()

    if disclose in (repr, str):
        shown = disclose(login)
        assert "canary" not in shown
    else:
        with pytest.raises(TypeError):
            disclose(login)
