"""The service starts on a provider table: its config and its startup checks.

Rows validate documents (and ``cli.main`` for the refusal a person reads) with a stand-in for the
pinned CLI and no installed core configuration, and run once per table in ``TABLES``; the builders
and the table descriptions are those of ``tests.support.service_table_support``.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import reduce
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.core_runtime_support import (
    no_installed_core_runtime,  # noqa: F401 - an autouse fixture
)
from tests.support.provider_test_support import CANARY_ACCESS_TOKEN
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
from tests.support.service_table_support import (
    ProviderTable,
    a_codex_config,
    the_refusal_of,
    the_served_runs,  # noqa: F401 - the fixture is named in its decorator
    the_table,  # noqa: F401 - the fixture is named in its decorator
    write_login,
    written_document,
)

from agent_runtime.secret_file import MAX_SECRET_FILE_BYTES
from agent_runtime_service.cli import main
from agent_runtime_service.config import Provider

# Startup: lines 3, 7, 14 and the matrix cells S1 to S10.


def test_a_table_starts_the_service_after_its_program_proves_its_pin(
    tmp_path: Path, served_runs: list[dict[str, Any]], table: ProviderTable
) -> None:
    document = table.document(tmp_path, table.service_fake(tmp_path), table.login_file(tmp_path))

    assert main(["--config", str(written_document(tmp_path, document))]) == 0
    assert len(served_runs) == 1


def test_a_program_reporting_another_version_stops_the_start_naming_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], table: ProviderTable
) -> None:
    binary = table.service_fake(tmp_path)
    document = table.document(tmp_path, binary, table.login_file(tmp_path))
    document["cli"][table.provider]["version"] = "0.164.0"

    refusal = refused_on_one_line(written_document(tmp_path, document), capsys)

    assert refusal == (
        f"cli.{table.provider}.version: {binary} reports {table.pin}, not the pinned 0.164.0"
    )


def test_a_table_without_its_login_is_refused_before_any_read(
    tmp_path: Path, table: ProviderTable
) -> None:
    open_login = table.login_file(tmp_path)
    open_login.chmod(0o644)
    document = table.document(tmp_path, table.service_fake(tmp_path), open_login)
    del document["cli"][table.provider]["token_file"]

    shown_refusal = the_refusal_of(document)
    assert f"cli.{table.provider}.token_file\n  Field required" in shown_refusal
    assert "open to group" not in shown_refusal


def a_directory_instead(table: ProviderTable, directory: Path) -> Path:
    (directory / table.login_name).mkdir()
    return directory / table.login_name


def a_login_that_is(
    contents: Callable[[ProviderTable], bytes], mode: int = 0o600
) -> Callable[[ProviderTable, Path], Path]:
    return lambda table, directory: write_login(directory / table.login_name, contents(table), mode)


LOGIN_FAULTS = [
    pytest.param(
        lambda table, directory: directory / table.login_name,
        lambda table: "cannot be read: No such file",
        id="missing",
    ),
    pytest.param(
        a_login_that_is(lambda table: table.login_document(None), 0o644),
        lambda table: "is open to group or others",
        id="mode-0644",
    ),
    pytest.param(a_directory_instead, lambda table: "is not a regular file", id="directory"),
    pytest.param(
        a_login_that_is(lambda table: b" " * (MAX_SECRET_FILE_BYTES + 1)),
        lambda table: f"is larger than {MAX_SECRET_FILE_BYTES} bytes",
        id="over-16-KiB",
    ),
    pytest.param(
        a_login_that_is(lambda table: b"sk-api-key-line-" + CANARY_ACCESS_TOKEN.encode()),
        lambda table: table.not_a_document,
        id="bare-key",
    ),
    pytest.param(
        a_login_that_is(lambda table: b"sk-ant-oat01-" + CANARY_ACCESS_TOKEN.encode()),
        lambda table: table.not_a_document,
        id="claude-token",
    ),
    pytest.param(
        a_login_that_is(lambda table: table.api_key_login),
        lambda table: table.incomplete,
        id="api-key-document",
    ),
    pytest.param(
        a_login_that_is(lambda table: table.malformed_login),
        lambda table: table.not_a_document,
        id="malformed-tokens",
    ),
    pytest.param(
        a_login_that_is(lambda table: table.empty_token_login),
        lambda table: table.incomplete,
        id="empty-token",
    ),
]


@pytest.mark.parametrize(("make", "reason_of"), LOGIN_FAULTS)
def test_a_login_file_that_is_not_a_sound_login_stops_the_start_naming_the_fault(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    table: ProviderTable,
    make: Callable[[ProviderTable, Path], Path],
    reason_of: Callable[[ProviderTable], str],
) -> None:
    login = make(table, tmp_path)
    document = table.document(tmp_path, table.service_fake(tmp_path), login)

    refusal = refused_on_one_line(written_document(tmp_path, document), capsys)

    assert refusal.startswith(f"cli.{table.provider}.token_file: {login} {reason_of(table)}")
    assert [c for c in table.canaries if c in refusal] == []


@pytest.mark.parametrize(("make", "reason_of"), LOGIN_FAULTS)
def test_a_refused_login_holds_no_token_in_what_the_refusal_holds(
    tmp_path: Path,
    table: ProviderTable,
    make: Callable[[ProviderTable, Path], Path],
    reason_of: Callable[[ProviderTable], str],
) -> None:
    document = table.document(tmp_path, table.service_fake(tmp_path), make(table, tmp_path))
    with pytest.raises(ValidationError) as refused:
        a_codex_config(document)

    held = everything_a_refusal_holds(refused.value)
    assert reason_of(table) in str(refused.value)
    assert not reaches_text(held, list(table.canaries))


def test_a_link_to_a_sound_login_loads_and_a_link_to_an_open_one_is_judged_by_its_target(
    tmp_path: Path, table: ProviderTable
) -> None:
    sound = table.login_file(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(sound)
    document = table.document(tmp_path, table.service_fake(tmp_path), link)

    assert getattr(a_codex_config(document).cli, table.provider.value).token_file == link

    sound.chmod(0o644)
    assert "is open to group or others" in the_refusal_of(document)


def test_a_claude_table_whose_file_holds_a_login_document_is_refused_as_json(
    tmp_path: Path, table: ProviderTable
) -> None:
    document = _service_config_document(
        TEST_ISSUER, None, None, token_file=table.login_file(tmp_path)
    )

    assert "holds a JSON document" in the_refusal_of(document)


def test_a_claude_table_and_a_table_load_together_and_list_their_programs_in_order(
    tmp_path: Path, table: ProviderTable
) -> None:
    config = a_codex_config(table.document_with_both(tmp_path))

    assert list(config.cli.programs()) == [Provider.ANTHROPIC, table.provider]


def test_an_installation_with_only_a_table_lists_only_its_program(
    tmp_path: Path, table: ProviderTable
) -> None:
    config = a_codex_config(
        table.document(tmp_path, table.service_fake(tmp_path), table.login_file(tmp_path))
    )

    assert list(config.cli.programs()) == [table.provider]


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
    table: ProviderTable,
    table_path: tuple[str, ...],
    key: str,
    value: object,
    refused_field: str,
) -> None:
    document = table.document(tmp_path, table.service_fake(tmp_path), table.login_file(tmp_path))
    reduce(lambda parent, name: parent[name], table_path, document)[key] = value

    refusal = refused_on_one_line(written_document(tmp_path, document), capsys)

    assert refusal.startswith(f"{refused_field}:")
