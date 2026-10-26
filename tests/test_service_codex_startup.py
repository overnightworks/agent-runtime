"""The service starts on a Codex profile: its config and its startup checks.

Config rows validate documents (and ``cli.main`` for the refusal a person reads) with a stand-in
for the pinned CLI and no installed core configuration; the shared builders and constants are
those of ``test_service_codex_profile``, whose rows run the profile.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    a_codex_login,
)
from pydantic import ValidationError
from service_support import (
    CLI_PROFILE,
    TEST_ISSUER,
    _service_config_document,
    everything_a_refusal_holds,
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    reaches_text,
)
from test_service_codex_profile import (
    CODEX_PIN,
    CODEX_PROFILE,
    LOGIN_NAME,
    a_config,
    a_document_with_both,
    a_login_file,
    a_service_fake,
    codex_document,
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
    served,  # noqa: F401 - a fixture, requested by name
    the_refusal_of,
    write_login,
    written,
)
from test_service_config import _refused_on_one_line

from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_service.cli import main
from agent_runtime_service.config import Provider

# Startup: lines 3, 7, 14 and the matrix cells S1 to S10.


def test_a_codex_profile_starts_the_service_after_its_program_proves_its_pin(
    tmp_path: Path,
    served: list[dict[str, Any]],  # noqa: F811
) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path))

    assert main(["--config", str(written(tmp_path, document))]) == 0
    assert len(served) == 1


def test_a_program_reporting_another_version_stops_the_start_naming_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = a_service_fake(tmp_path)
    document = codex_document(tmp_path, binary, a_login_file(tmp_path))
    document["cli"]["codex"]["version"] = "0.164.0"

    refusal = _refused_on_one_line(written(tmp_path, document), capsys)

    assert refusal == f"cli.codex.version: {binary} reports {CODEX_PIN}, not the pinned 0.164.0"


@pytest.mark.parametrize(
    ("without", "refusal"),
    [
        pytest.param(
            "token_file",
            f"profiles.{CODEX_PROFILE}.token_file: required for a codex profile",
            id="token-file",
        ),
        pytest.param("cli.codex", "cli.codex: required for a codex profile", id="program-table"),
    ],
)
def test_a_codex_profile_without_its_login_or_program_table_is_refused_before_any_read(
    tmp_path: Path, without: str, refusal: str
) -> None:
    open_login = a_login_file(tmp_path)
    open_login.chmod(0o644)
    document = codex_document(tmp_path, a_service_fake(tmp_path), open_login)
    if without == "token_file":
        del document["profiles"][CODEX_PROFILE]["token_file"]
    else:
        del document["cli"]["codex"]

    shown_refusal = the_refusal_of(document)
    assert refusal in shown_refusal
    assert "open to group" not in shown_refusal


def a_directory_instead(directory: Path) -> Path:
    (directory / LOGIN_NAME).mkdir()
    return directory / LOGIN_NAME


def a_login_that_is(contents: bytes, mode: int = 0o600) -> Callable[[Path], Path]:
    return lambda directory: write_login(directory / LOGIN_NAME, contents, mode)


NOT_A_DOCUMENT = "is not a Codex login document"
NO_TOKENS = (
    "is not a complete Codex sign-in; an API key (OPENAI_API_KEY) is not a subscription login"
)
LOGIN_FAULTS = [
    pytest.param(
        lambda directory: directory / LOGIN_NAME, "cannot be read: No such file", id="missing"
    ),
    pytest.param(
        a_login_that_is(a_codex_login(), 0o644), "is open to group or others", id="mode-0644"
    ),
    pytest.param(a_directory_instead, "is not a regular file", id="directory"),
    pytest.param(
        a_login_that_is(b" " * (MAX_SECRET_FILE_BYTES + 1)),
        f"is larger than {MAX_SECRET_FILE_BYTES} bytes",
        id="over-16-KiB",
    ),
    pytest.param(
        a_login_that_is(b"sk-api-key-line-" + CANARY_ACCESS_TOKEN.encode()),
        NOT_A_DOCUMENT,
        id="bare-key",
    ),
    pytest.param(
        a_login_that_is(b"sk-ant-oat01-" + CANARY_ACCESS_TOKEN.encode()),
        NOT_A_DOCUMENT,
        id="claude-token",
    ),
    pytest.param(
        a_login_that_is(json.dumps({"OPENAI_API_KEY": CANARY_ACCESS_TOKEN}).encode()),
        NO_TOKENS,
        id="api-key-document",
    ),
    pytest.param(
        a_login_that_is(a_codex_login(tokens={"access_token": 5})),
        NOT_A_DOCUMENT,
        id="malformed-tokens",
    ),
    pytest.param(
        a_login_that_is(a_codex_login(tokens={"id_token": ""})), NO_TOKENS, id="empty-token"
    ),
]


@pytest.mark.parametrize(("make", "reason"), LOGIN_FAULTS)
def test_a_login_file_that_is_not_a_sound_codex_login_stops_the_start_naming_the_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], make: Callable[[Path], Path], reason: str
) -> None:
    login = make(tmp_path)
    document = codex_document(tmp_path, a_service_fake(tmp_path), login)

    refusal = _refused_on_one_line(written(tmp_path, document), capsys)

    assert refusal.startswith(f"profiles.{CODEX_PROFILE}.token_file: {login} {reason}")
    assert [c for c in (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN) if c in refusal] == []


@pytest.mark.parametrize(("make", "reason"), LOGIN_FAULTS)
def test_a_refused_login_holds_no_token_in_what_the_refusal_holds(
    tmp_path: Path, make: Callable[[Path], Path], reason: str
) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), make(tmp_path))
    with pytest.raises(ValidationError) as refused:
        a_config(document)

    held = everything_a_refusal_holds(refused.value)
    assert reason in str(refused.value)
    assert not reaches_text(held, [CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN])


def test_a_link_to_a_sound_login_loads_and_a_link_to_an_open_one_is_judged_by_its_target(
    tmp_path: Path,
) -> None:
    sound = a_login_file(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(sound)
    document = codex_document(tmp_path, a_service_fake(tmp_path), link)

    assert a_config(document).profiles[CODEX_PROFILE].token_file == link

    sound.chmod(0o644)
    assert "is open to group or others" in the_refusal_of(document)


def test_a_claude_profile_whose_file_holds_a_codex_document_is_refused_as_json(
    tmp_path: Path,
) -> None:
    document = _service_config_document(TEST_ISSUER, None, None, token_file=a_login_file(tmp_path))

    assert "holds a JSON document" in the_refusal_of(document)


def test_a_grok_profile_is_refused_naming_its_provider(tmp_path: Path) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path))
    document["profiles"] = {"grok-main": {"provider": "grok", "background": False}}

    assert (
        "profiles.grok-main.provider: grok has no subscription route yet;"
        " only claude and codex can have a profile"
    ) in the_refusal_of(document)


def test_a_second_codex_profile_is_refused(tmp_path: Path) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path))
    document["profiles"]["codex-spare"] = dict(document["profiles"][CODEX_PROFILE])

    assert "profiles.codex-spare.provider: codex already has profile 'codex-main'" in (
        the_refusal_of(document)
    )


def test_a_claude_and_a_codex_profile_load_together_and_list_their_programs_in_order(
    tmp_path: Path,
) -> None:
    config = a_config(a_document_with_both(tmp_path))

    assert config.cli is not None
    assert list(config.cli.programs()) == [Provider.CLAUDE, Provider.CODEX]


def test_a_codex_only_installation_lists_only_its_codex_program(tmp_path: Path) -> None:
    config = a_config(codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path)))

    assert config.cli is not None
    assert list(config.cli.programs()) == [Provider.CODEX]


# The default profile (ruled 26.10.2026, line "no model asked").


def test_a_lone_profile_is_the_default_whether_or_not_it_is_marked(tmp_path: Path) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_login_file(tmp_path))
    marked_off = dict(document["profiles"][CODEX_PROFILE], default=False)
    unmarked = a_config(document).profiles[CODEX_PROFILE].default
    document["profiles"][CODEX_PROFILE] = marked_off

    assert (unmarked, a_config(document).profiles[CODEX_PROFILE].default) == (True, True)


@pytest.mark.parametrize(
    ("marks", "refusal"),
    [
        pytest.param(
            (False, False),
            f"profiles.{CLI_PROFILE}.default: one profile must be marked default = true",
            id="none",
        ),
        pytest.param(
            (True, True),
            f"profiles.{CODEX_PROFILE}.default: only one profile may be marked default = true",
            id="two",
        ),
        pytest.param(
            (False, "yes"), f"profiles.{CODEX_PROFILE}.default", id="not-a-boolean-is-named"
        ),
    ],
)
def test_two_profiles_need_exactly_one_default(
    tmp_path: Path, marks: tuple[bool, bool | str], refusal: str
) -> None:
    document = a_document_with_both(tmp_path)
    document["profiles"][CLI_PROFILE]["default"], document["profiles"][CODEX_PROFILE]["default"] = (
        marks
    )

    assert refusal in the_refusal_of(document)
