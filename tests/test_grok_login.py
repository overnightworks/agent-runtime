"""The copy of the Grok login a child may hold (#255 E10, E10a; defect:#442)."""

from __future__ import annotations

import copy as copying
import json
import pickle
from pathlib import Path

import pytest
from tests.support.codex_support import a_codex_access_token
from tests.support.provider_test_support import (
    GROK_LOGGED_IN,
    SAMPLE_TOOL_CATALOG,
    override_grok_runtime,
)

from agent_runtime import process
from agent_runtime.errors import ProviderUnavailableError, SafeRouteReasonCode
from agent_runtime.grok.transport import GrokCliToolTransport
from agent_runtime.grok_login import (
    GrokLoginVerdict,
    RenewableGrokLogin,
    assess_grok_login,
    blanked_copy,
    read_grok_timing,
    renewable_grok_login,
)
from agent_runtime.login_renewal import RenewalRefusal
from agent_runtime.login_timing import AccessTokenTiming, UnreadableTiming
from agent_runtime.process import (
    CliLoginUnusableError,
    CliRunOutcome,
    CliRunReason,
    clear_agent_cli_caches,
    grok_cli_status,
    grok_cli_token_is_present,
)

A_REFRESH_TOKEN = "canary-grok-refresh-token"
A_KEPT_MEMBERS = {"key": "grok-synthetic-key", "expires_at": 1234, "scopes": ["a", "b"]}


def a_document(**realm: object) -> bytes:
    return json.dumps({"realm": {"key": "grok-synthetic-key", **realm}}).encode()


@pytest.mark.parametrize("name", ["refresh_token", "refreshToken", "refresh"])
@pytest.mark.parametrize("depth", [1, 2])
def test_every_renewal_name_is_blanked_at_every_depth(name: str, depth: int) -> None:
    member = {name: A_REFRESH_TOKEN, **A_KEPT_MEMBERS}
    realm = {"key": "grok-synthetic-key", **(member if depth == 1 else {"nested": member})}

    login = assess_grok_login(json.dumps({"realm": realm}).encode())

    assert login.verdict is GrokLoginVerdict.USABLE
    assert login.blanked_document is not None
    blanked_member = {name: "", **A_KEPT_MEMBERS}
    blanked_realm = blanked_member if depth == 1 else {"nested": blanked_member}
    assert json.loads(login.blanked_document) == {
        "realm": {"key": "grok-synthetic-key", **blanked_realm}
    }
    assert A_REFRESH_TOKEN.encode() not in login.blanked_document


def test_a_renewal_member_inside_a_list_is_blanked() -> None:
    document = a_document(realms=[{"refresh_token": A_REFRESH_TOKEN}])

    copy = blanked_copy(document)

    assert copy is not None
    assert json.loads(copy)["realm"]["realms"] == [{"refresh_token": ""}]


def test_a_member_whose_name_only_resembles_a_renewal_name_is_kept() -> None:
    kept = {"refresh_token_expiry": 5, "Refresh": "kept", "x.refresh_token": "kept"}

    copy = blanked_copy(a_document(**kept))

    assert copy is not None
    assert json.loads(copy)["realm"] == {"key": "grok-synthetic-key", **kept}


@pytest.mark.parametrize(
    ("document", "verdict"),
    [
        pytest.param(b'{"a": {"key": "k"}}', GrokLoginVerdict.USABLE, id="one-realm-with-key"),
        pytest.param(
            b'{"a": {}, "b": {"key": "k"}}',
            GrokLoginVerdict.USABLE,
            id="second-realm-has-key",
        ),
        pytest.param(b"{}", GrokLoginVerdict.INCOMPLETE, id="no-realm"),
        pytest.param(b'{"a": {}}', GrokLoginVerdict.INCOMPLETE, id="realm-without-key"),
        pytest.param(b'{"a": {"key": ""}}', GrokLoginVerdict.INCOMPLETE, id="empty-key"),
        pytest.param(b'{"a": {"key": null}}', GrokLoginVerdict.INCOMPLETE, id="null-key"),
        pytest.param(b'{"a": {"key": 1}}', GrokLoginVerdict.MALFORMED, id="key-not-a-string"),
        pytest.param(b'{"realm": []}', GrokLoginVerdict.INCOMPLETE, id="realm-is-an-array"),
        pytest.param(b'{"realm": "x"}', GrokLoginVerdict.INCOMPLETE, id="realm-is-a-string"),
        pytest.param(
            b'{"a": {"key": "k"}, "b": 1}',
            GrokLoginVerdict.USABLE,
            id="usable-then-non-realm",
        ),
        pytest.param(
            b'{"a": {"key": "k"}, "b": {"key": 1}}',
            GrokLoginVerdict.MALFORMED,
            id="usable-then-bad-key",
        ),
        pytest.param(b"not json", GrokLoginVerdict.MALFORMED, id="not-json"),
        pytest.param(b"[]", GrokLoginVerdict.MALFORMED, id="array"),
        pytest.param(b'"refresh_token"', GrokLoginVerdict.MALFORMED, id="string"),
        pytest.param(b"null", GrokLoginVerdict.MALFORMED, id="null"),
        pytest.param(b'{"a": "\xff"}', GrokLoginVerdict.MALFORMED, id="invalid-utf8"),
        pytest.param(
            '{"a": {"key": "k"}}'.encode("utf-16"),
            GrokLoginVerdict.MALFORMED,
            id="utf-16",
        ),
        pytest.param(
            b'\xef\xbb\xbf{"a": {"key": "k"}}',
            GrokLoginVerdict.MALFORMED,
            id="utf-8-byte-order-mark",
        ),
        pytest.param(b"[" * 100_000, GrokLoginVerdict.MALFORMED, id="excessive-nesting"),
        pytest.param(
            b'{"a": {"key": "k", "x": ' + b"[" * 100_000 + b"}}",
            GrokLoginVerdict.MALFORMED,
            id="excessive-nesting-in-a-realm",
        ),
        pytest.param(
            b'{"a": {"key": "k", "x": ' + b"[" * 3000 + b"]" * 3000 + b"}}",
            GrokLoginVerdict.MALFORMED,
            id="balanced-nesting-beyond-the-recursion-limit",
        ),
    ],
)
def test_a_login_document_has_one_verdict_and_only_a_usable_one_has_a_copy(
    document: bytes,
    verdict: GrokLoginVerdict,
) -> None:
    login = assess_grok_login(document)

    assert login.verdict is verdict
    assert (login.blanked_document is not None) is (verdict is GrokLoginVerdict.USABLE)
    assert blanked_copy(document) == login.blanked_document


def test_a_login_neither_shows_nor_pickles_its_document() -> None:
    login = assess_grok_login(a_document(refresh_token=A_REFRESH_TOKEN))

    assert "grok-synthetic-key" not in repr(login)
    with pytest.raises(TypeError):
        pickle.dumps(login)


@pytest.mark.parametrize(
    ("document", "readiness"),
    [
        pytest.param(b'{"realm": {"key": "k"}}', True, id="usable"),
        pytest.param(b'{"realm": {"key": ""}}', False, id="incomplete"),
        pytest.param(b'{"realm": []}', False, id="realm-is-an-array"),
    ],
)
def test_readiness_and_copy_agree_on_a_usable_or_incomplete_login(
    tmp_path: Path,
    document: bytes,
    readiness: bool,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(document)
    override_grok_runtime(token_file=source)

    assert grok_cli_token_is_present() is readiness
    assert (blanked_copy(document) is not None) is readiness


@pytest.mark.parametrize(
    "document",
    [b"[]", b"not json", b'{"a": {"key": 1}}'],
    ids=["array", "not-json", "key-not-a-string"],
)
def test_a_malformed_login_is_refused_by_readiness_and_gets_no_copy(
    tmp_path: Path,
    document: bytes,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(document)
    override_grok_runtime(token_file=source)

    with pytest.raises(CliLoginUnusableError):
        grok_cli_token_is_present()
    assert blanked_copy(document) is None


@pytest.mark.parametrize("document", [b"[]", b"not json"], ids=["array", "not-json"])
def test_grok_turn_whose_login_is_not_a_json_object_is_logged_out(
    tmp_path: Path,
    document: bytes,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(document)
    override_grok_runtime(token_file=source)

    with pytest.raises(ProviderUnavailableError) as raised:
        GrokCliToolTransport(model="grok-test", catalog=SAMPLE_TOOL_CATALOG)

    assert raised.value.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED


@pytest.mark.parametrize("document", [b"[]", b"not json"], ids=["array", "not-json"])
def test_the_grok_probe_hands_its_child_no_login_when_the_source_is_not_a_json_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: bytes,
) -> None:
    auth_file = tmp_path / "grok-auth.json"
    auth_file.write_bytes(document)
    override_grok_runtime(token_file=auth_file)
    homes_with_a_login: list[bool] = []

    def run_cli_bounded(child, **kwargs):
        has_login = (Path(child.environment["GROK_HOME"]) / "auth.json").exists()
        homes_with_a_login.append(has_login)
        kwargs["on_reaped"](0, False)
        return CliRunOutcome(
            started=True,
            spawn_error=None,
            returncode=0,
            stdout=GROK_LOGGED_IN if has_login else "You are not authenticated.\n",
            stderr="",
            complete=True,
            became_zombie=False,
            reason=CliRunReason.COMPLETE,
        )

    monkeypatch.setattr(process, "run_cli_bounded", run_cli_bounded)
    clear_agent_cli_caches()

    assert grok_cli_status().login.logged_in is False
    assert homes_with_a_login == [False]


def a_key(**claims: object) -> str:
    return a_codex_access_token({"iat": 1_800_000_000, "exp": 1_800_021_600, **claims})


def a_login_with(*keys: str, **realm: object) -> bytes:
    return json.dumps(
        {
            f"realm-{index}": {"key": key, "refresh_token": A_REFRESH_TOKEN, **realm}
            for index, key in enumerate(keys)
        }
    ).encode()


def test_a_usable_login_with_a_refresh_member_is_renewable_and_shows_nothing() -> None:
    access_token = a_key()
    login = renewable_grok_login(a_login_with(access_token))

    assert isinstance(login, RenewableGrokLogin)
    for canary in (A_REFRESH_TOKEN, access_token):
        assert canary not in repr(login) + str(login)
    for disclose in (pickle.dumps, copying.copy, copying.deepcopy):
        with pytest.raises(TypeError) as refusal:
            disclose(login)
        for canary in (A_REFRESH_TOKEN, access_token):
            assert canary not in str(refusal.value) + repr(refusal.value)


@pytest.mark.parametrize(
    ("document", "refusal"),
    [
        pytest.param(
            json.dumps({"realm": {"key": "k", "refresh_token": ""}}).encode(),
            RenewalRefusal.NO_REFRESH_TOKEN,
            id="blanked-copy",
        ),
        pytest.param(
            json.dumps({"realm": {"key": "k"}}).encode(),
            RenewalRefusal.NO_REFRESH_TOKEN,
            id="no-renewal-member",
        ),
        pytest.param(
            blanked_copy(
                json.dumps(
                    {
                        "realm": {
                            "key": "k",
                            "refresh_token": "a",
                            "nested": [{"refreshToken": "b"}, {"inner": [{"refresh": "c"}]}],
                        }
                    }
                ).encode()
            ),
            RenewalRefusal.NO_REFRESH_TOKEN,
            id="blanked-copy-of-a-nested-list",
        ),
        pytest.param(b"not json", RenewalRefusal.NOT_USABLE, id="malformed"),
        pytest.param(
            json.dumps({"realm": {"refresh_token": "r"}}).encode(),
            RenewalRefusal.NOT_USABLE,
            id="no-key",
        ),
    ],
)
def test_a_login_that_cannot_renew_is_refused_with_its_cause(
    document: bytes, refusal: RenewalRefusal
) -> None:
    assert renewable_grok_login(document) is refusal


@pytest.mark.parametrize(
    "expires_at",
    ["2026-10-30T12:00:00Z", 1_800_021_600_000, 5],
    ids=["iso-string", "milliseconds", "contradicting"],
)
def test_the_timing_is_the_keys_claims_and_expires_at_has_no_effect(expires_at: object) -> None:
    timing = read_grok_timing(a_login_with(a_key(), expires_at=expires_at), 1_800_000_000)

    assert timing == AccessTokenTiming(1_800_000_000, 1_800_021_600)


@pytest.mark.parametrize(
    ("document", "cause"),
    [
        pytest.param(json.dumps({"realm": {"key": ""}}).encode(), "NO_ACCESS_TOKEN", id="no-key"),
        pytest.param(b"[]", "NO_ACCESS_TOKEN", id="not-an-object"),
        pytest.param(b"\xff\xfe", "NO_ACCESS_TOKEN", id="hostile-bytes"),
        pytest.param(a_login_with(a_key(), a_key()), "SEVERAL_ACCESS_TOKENS", id="two-keys"),
        pytest.param(a_login_with("opaque"), "NOT_A_TOKEN", id="not-a-jwt"),
        pytest.param(a_login_with(a_key(exp=1.5e9)), "WRONG_TYPE", id="float-claim"),
    ],
)
def test_a_timing_that_cannot_be_read_names_its_cause_and_never_raises(
    document: bytes, cause: str
) -> None:
    assert read_grok_timing(document, 1_800_000_000) is UnreadableTiming[cause]


def test_a_renewal_member_held_only_in_a_list_makes_a_login_renewable() -> None:
    document = json.dumps({"realm": {"key": "k", "nested": [{"refresh": "r"}]}}).encode()

    assert isinstance(renewable_grok_login(document), RenewableGrokLogin)


@pytest.mark.parametrize(
    ("other", "shares"),
    [
        pytest.param(a_login_with("another-key"), True, id="same-member-new-key"),
        pytest.param(
            json.dumps({"realm-0": {"key": "k", "refresh_token": "rotated"}}).encode(),
            False,
            id="rotated-member",
        ),
    ],
)
def test_logins_share_a_refresh_token_when_every_renewal_member_is_equal(
    other: bytes, shares: bool
) -> None:
    login = renewable_grok_login(a_login_with("old-key"))
    newer = renewable_grok_login(other)
    assert isinstance(login, RenewableGrokLogin)
    assert isinstance(newer, RenewableGrokLogin)

    assert login.shares_refresh_token_with(newer) is shares


def test_logins_do_not_share_a_refresh_token_when_one_of_several_members_rotates() -> None:
    def login_with(rotated_value: str) -> RenewableGrokLogin:
        members = {"refresh_token": "a", "refresh": rotated_value}
        login = renewable_grok_login(json.dumps({"realm-0": {"key": "k", **members}}).encode())
        assert isinstance(login, RenewableGrokLogin)
        return login

    assert not login_with("old").shares_refresh_token_with(login_with("new"))


def test_logins_with_the_same_renewal_members_in_another_order_share_a_refresh_token() -> None:
    def login_with(**members: str) -> RenewableGrokLogin:
        login = renewable_grok_login(json.dumps({"realm-0": {"key": "k", **members}}).encode())
        assert isinstance(login, RenewableGrokLogin)
        return login

    first = login_with(refresh_token="a", refresh="b")
    reordered = login_with(refresh="b", refresh_token="a")

    assert first.shares_refresh_token_with(reordered)
