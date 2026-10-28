"""The service starts on a Codex table: its config and its startup checks.

Config rows validate documents (and ``cli.main`` for the refusal a person reads) with a stand-in
for the pinned CLI and no installed core configuration; the shared builders and constants are
those of ``test_cli_runner_codex_profile``, whose rows run the table.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import reduce
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.provider_test_support import (
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    a_codex_login,
)
from tests.support.service_codex_support import (
    CODEX_LOGIN_NAME,
    CODEX_PIN,
    a_codex_config,
    a_codex_login_file,
    a_document_with_both,
    a_service_fake,
    codex_document,
    the_refusal_of,
    the_served_runs,  # noqa: F401 - the fixture is named in its decorator
    write_login,
    written_document,
)
from tests.support.service_config_support import (
    refused_on_one_line,
)
from tests.support.service_support import (
    TEST_ISSUER,
    _service_config_document,
    everything_a_refusal_holds,
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    reaches_text,
)

from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_service.cli import main
from agent_runtime_service.config import Provider

# Startup: lines 3, 7, 14 and the matrix cells S1 to S10.


def test_a_codex_table_starts_the_service_after_its_program_proves_its_pin(
    tmp_path: Path,
    served_runs: list[dict[str, Any]],
) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_codex_login_file(tmp_path))

    assert main(["--config", str(written_document(tmp_path, document))]) == 0
    assert len(served_runs) == 1


def test_a_program_reporting_another_version_stops_the_start_naming_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = a_service_fake(tmp_path)
    document = codex_document(tmp_path, binary, a_codex_login_file(tmp_path))
    document["cli"][Provider.OPENAI]["version"] = "0.164.0"

    refusal = refused_on_one_line(written_document(tmp_path, document), capsys)

    assert refusal == f"cli.openai.version: {binary} reports {CODEX_PIN}, not the pinned 0.164.0"


def test_a_codex_table_without_its_login_is_refused_before_any_read(tmp_path: Path) -> None:
    open_login = a_codex_login_file(tmp_path)
    open_login.chmod(0o644)
    document = codex_document(tmp_path, a_service_fake(tmp_path), open_login)
    del document["cli"][Provider.OPENAI]["token_file"]

    shown_refusal = the_refusal_of(document)
    assert "cli.openai.token_file\n  Field required" in shown_refusal
    assert "open to group" not in shown_refusal


def a_directory_instead(directory: Path) -> Path:
    (directory / CODEX_LOGIN_NAME).mkdir()
    return directory / CODEX_LOGIN_NAME


def a_login_that_is(contents: bytes, mode: int = 0o600) -> Callable[[Path], Path]:
    return lambda directory: write_login(directory / CODEX_LOGIN_NAME, contents, mode)


NOT_A_DOCUMENT = "is not a Codex login document"
NO_TOKENS = (
    "is not a complete Codex sign-in; an API key (OPENAI_API_KEY) is not a subscription login"
)
LOGIN_FAULTS = [
    pytest.param(
        lambda directory: directory / CODEX_LOGIN_NAME, "cannot be read: No such file", id="missing"
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

    refusal = refused_on_one_line(written_document(tmp_path, document), capsys)

    assert refusal.startswith(f"cli.openai.token_file: {login} {reason}")
    assert [c for c in (CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN) if c in refusal] == []


@pytest.mark.parametrize(("make", "reason"), LOGIN_FAULTS)
def test_a_refused_login_holds_no_token_in_what_the_refusal_holds(
    tmp_path: Path, make: Callable[[Path], Path], reason: str
) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), make(tmp_path))
    with pytest.raises(ValidationError) as refused:
        a_codex_config(document)

    held = everything_a_refusal_holds(refused.value)
    assert reason in str(refused.value)
    assert not reaches_text(held, [CANARY_ACCESS_TOKEN, CANARY_ID_TOKEN])


def test_a_link_to_a_sound_login_loads_and_a_link_to_an_open_one_is_judged_by_its_target(
    tmp_path: Path,
) -> None:
    sound = a_codex_login_file(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(sound)
    document = codex_document(tmp_path, a_service_fake(tmp_path), link)

    assert a_codex_config(document).cli.openai.token_file == link

    sound.chmod(0o644)
    assert "is open to group or others" in the_refusal_of(document)


def test_a_claude_table_whose_file_holds_a_codex_document_is_refused_as_json(
    tmp_path: Path,
) -> None:
    document = _service_config_document(
        TEST_ISSUER, None, None, token_file=a_codex_login_file(tmp_path)
    )

    assert "holds a JSON document" in the_refusal_of(document)


def test_a_grok_table_is_refused_as_an_unknown_key(tmp_path: Path) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_codex_login_file(tmp_path))
    document["cli"]["grok"] = document["cli"][Provider.OPENAI]

    assert "cli.grok" in the_refusal_of(document)


def test_a_claude_and_a_codex_table_load_together_and_list_their_programs_in_order(
    tmp_path: Path,
) -> None:
    config = a_codex_config(a_document_with_both(tmp_path))

    assert list(config.cli.programs()) == [Provider.ANTHROPIC, Provider.OPENAI]


def test_a_codex_only_installation_lists_only_its_codex_program(tmp_path: Path) -> None:
    config = a_codex_config(
        codex_document(tmp_path, a_service_fake(tmp_path), a_codex_login_file(tmp_path))
    )

    assert list(config.cli.programs()) == [Provider.OPENAI]


@pytest.mark.parametrize(
    ("table_path", "key", "value", "refused_field"),
    [
        pytest.param((), "profiles", {"a": {"provider": "codex"}}, "profiles", id="profiles-table"),
        pytest.param((), "route", {"api_alias": "assistant-api"}, "route", id="route-table"),
    ],
)
def test_a_leftover_profiles_or_route_table_stops_the_start_naming_its_field(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    table_path: tuple[str, ...],
    key: str,
    value: object,
    refused_field: str,
) -> None:
    document = codex_document(tmp_path, a_service_fake(tmp_path), a_codex_login_file(tmp_path))
    reduce(lambda table, name: table[name], table_path, document)[key] = value

    refusal = refused_on_one_line(written_document(tmp_path, document), capsys)

    assert refusal.startswith(f"{refused_field}:")
