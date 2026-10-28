"""The token pairs the entrance refuses, and the code each is refused with."""

import time
from collections.abc import Callable

import pytest
from tests.support.service_support import (
    PRESENTATOR_CLIENT,
    SONGMAKER_CLIENT,
    TokenMint,
    shared_mint,
)

from agent_runtime_client.wire import RejectionCode

MINT = shared_mint()
OTHER_MINT = TokenMint()

TokenFor = Callable[[], str | None]


def _no_token() -> None:
    return None


def expired_workload() -> str:
    now = int(time.time())
    return MINT.workload(iat=now - 400, exp=now - 100)


def _long_lived_workload() -> str:
    now = int(time.time())
    return MINT.workload(iat=now, exp=now + 301)


def _user_with_the_workload_subject() -> str:
    return MINT.user(sub=f"service-account-user-of-{SONGMAKER_CLIENT}")


UNAUTHORIZED = RejectionCode.UNAUTHORIZED
FORBIDDEN = RejectionCode.FORBIDDEN

TOKEN_REJECTIONS = [
    pytest.param(_no_token, _no_token, UNAUTHORIZED, id="no-tokens"),
    pytest.param(_no_token, MINT.user, UNAUTHORIZED, id="no-workload-token"),
    pytest.param(MINT.workload, _no_token, UNAUTHORIZED, id="no-user-token"),
    pytest.param(lambda: "not.a.jwt", MINT.user, UNAUTHORIZED, id="garbage-workload-token"),
    pytest.param(OTHER_MINT.workload, MINT.user, UNAUTHORIZED, id="workload-of-an-unknown-key"),
    pytest.param(expired_workload, MINT.user, UNAUTHORIZED, id="expired-workload-token"),
    pytest.param(
        MINT.workload, lambda: MINT.user(aud="account"), UNAUTHORIZED, id="user-wrong-audience"
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(iss="https://elsewhere.test/realms/x"),
        UNAUTHORIZED,
        id="user-token-wrong-issuer",
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(groups="/subscription-members"),
        UNAUTHORIZED,
        id="groups-not-a-list",
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(groups=["/subscription-members", 7]),
        UNAUTHORIZED,
        id="groups-with-a-non-string",
    ),
    pytest.param(
        MINT.workload, lambda: MINT.user(sub=None), UNAUTHORIZED, id="user-token-without-subject"
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(sub="user-\ud800"),
        UNAUTHORIZED,
        id="user-subject-with-a-lone-surrogate",
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(azp=PRESENTATOR_CLIENT, sub="user-\ud800"),
        UNAUTHORIZED,
        id="lone-surrogate-answers-before-the-azp-mismatch",
    ),
    pytest.param(
        lambda: MINT.workload(client="unknown-app"),
        lambda: MINT.user(azp="unknown-app", sub="user-\ud800"),
        UNAUTHORIZED,
        id="lone-surrogate-answers-before-an-unregistered-client",
    ),
    pytest.param(
        lambda: MINT.workload(realm_access={"roles": ["default-roles-agent-test"]}),
        MINT.user,
        FORBIDDEN,
        id="workload-without-the-workload-role",
    ),
    pytest.param(
        lambda: MINT.workload(client_id=PRESENTATOR_CLIENT),
        MINT.user,
        FORBIDDEN,
        id="workload-client-id-differs-from-azp",
    ),
    pytest.param(
        lambda: MINT.workload(client_id=None), MINT.user, FORBIDDEN, id="workload-without-client-id"
    ),
    pytest.param(
        lambda: MINT.workload(client_id=""),
        MINT.user,
        FORBIDDEN,
        id="workload-with-empty-client-id",
    ),
    pytest.param(lambda: MINT.workload(azp=None), MINT.user, FORBIDDEN, id="workload-without-azp"),
    pytest.param(lambda: MINT.workload(azp=""), MINT.user, FORBIDDEN, id="workload-with-empty-azp"),
    pytest.param(MINT.workload, lambda: MINT.user(azp=None), FORBIDDEN, id="user-without-azp"),
    pytest.param(MINT.workload, lambda: MINT.user(azp=""), FORBIDDEN, id="user-with-empty-azp"),
    pytest.param(
        lambda: MINT.workload(client="unknown-app"),
        lambda: MINT.user(azp="unknown-app"),
        FORBIDDEN,
        id="workload-of-an-unregistered-client",
    ),
    pytest.param(_long_lived_workload, MINT.user, FORBIDDEN, id="workload-living-over-300-s"),
    pytest.param(
        lambda: MINT.workload(sub=""), MINT.user, UNAUTHORIZED, id="workload-with-empty-subject"
    ),
    pytest.param(
        lambda: MINT.workload(sub=None), MINT.user, FORBIDDEN, id="workload-without-subject"
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(azp=PRESENTATOR_CLIENT),
        FORBIDDEN,
        id="user-logged-in-through-another-app",
    ),
    pytest.param(
        MINT.workload,
        _user_with_the_workload_subject,
        FORBIDDEN,
        id="user-subject-equals-workload-subject",
    ),
    pytest.param(
        lambda: MINT.workload(realm_access={"roles": []}),
        lambda: "not.a.jwt",
        UNAUTHORIZED,
        id="an-unusable-token-answers-before-a-forbidden-one",
    ),
]
