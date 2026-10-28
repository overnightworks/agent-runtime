"""The files a run returns on ``succeeded``, owned by ``agent_runtime_client.wire``.

A file is a name and its bytes; a run may return none, and may return no words
when it returns a file. The wire owns the name rules, the canonical base64 of the
bytes and the ceiling on all contents together.
"""

from __future__ import annotations

import base64
import json

import pytest
from pydantic import ValidationError
from tests.support.wire_builders import succeeded, wire

from agent_runtime_client.run_stream import StreamViolation, ViolationKind, parse_event
from agent_runtime_client.wire import (
    MAX_RUN_FILE_NAME_BYTES,
    MAX_RUN_FILES_BYTES,
    RunFile,
    Succeeded,
)

SECRET_MARKER = "SECRETMARKER"


def _file(name: str = "report.txt", content: bytes = b"la la") -> RunFile:
    return RunFile(name=name, content=content)


def _raw(files: list[dict[str, object]], text: str = "Done.") -> dict[str, object]:
    return wire("succeeded", 5, text=text, files=files)


def _violation(raw: dict[str, object]) -> StreamViolation:
    violation = parse_event(raw)
    assert isinstance(violation, StreamViolation)
    return violation


@pytest.mark.parametrize(
    "files",
    [
        pytest.param((), id="no file"),
        pytest.param((_file(),), id="one file"),
        pytest.param((_file("b"), _file("a", b""), _file("c", b"\x00\xff")), id="order kept"),
    ],
)
def test_a_succeeded_event_survives_a_json_round_trip(files: tuple[RunFile, ...]) -> None:
    event = succeeded(5, "Done.", files)

    assert parse_event(json.loads(event.model_dump_json())) == event


def test_a_file_travels_as_a_name_and_standard_base64() -> None:
    dumped = json.loads(succeeded(5, "Done.", (_file(content=b"\xfb\xff"),)).model_dump_json())

    assert dumped["files"] == [{"name": "report.txt", "content": "+/8="}]


def test_files_are_required() -> None:
    raw = _raw([])
    del raw["files"]

    assert _violation(raw).kind is ViolationKind.SCHEMA_MISMATCH


@pytest.mark.parametrize(
    ("text", "files", "accepted"),
    [
        pytest.param("", (_file(),), True, id="a file and no words"),
        pytest.param("", (), False, id="no file and no words"),
        pytest.param("words", (), True, id="words and no file"),
    ],
)
def test_words_are_required_only_without_files(
    text: str, files: tuple[RunFile, ...], accepted: bool
) -> None:
    raw = _raw([{"name": file.name, "content": "bGEgbGE="} for file in files], text=text)

    assert isinstance(parse_event(raw), Succeeded) is accepted
    if not accepted:
        with pytest.raises(ValidationError):
            succeeded(5, text, files)


def test_two_files_with_one_name_are_refused() -> None:
    with pytest.raises(ValidationError, match="unique"):
        succeeded(5, "Done.", (_file("a"), _file("a", b"other")))
    one = {"name": "a", "content": ""}

    assert _violation(_raw([one, one])).kind is ViolationKind.SCHEMA_MISMATCH


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dot dot"),
        pytest.param("a/b", id="slash"),
        pytest.param("a\0b", id="NUL"),
        pytest.param("x" * (MAX_RUN_FILE_NAME_BYTES + 1), id="256 bytes"),
        pytest.param("x" * (MAX_RUN_FILE_NAME_BYTES - 1) + "ä", id="254 bytes and a 2-byte char"),
        pytest.param("a\ud800", id="lone surrogate"),
    ],
)
def test_a_name_that_is_not_one_path_component_of_valid_size_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        _file(name)
    assert _violation(_raw([{"name": name, "content": ""}])).kind is ViolationKind.SCHEMA_MISMATCH


@pytest.mark.parametrize("name", ["x" * MAX_RUN_FILE_NAME_BYTES, "x" * 253 + "ä", "...", "a b"])
def test_a_name_of_at_most_255_bytes_is_accepted(name: str) -> None:
    assert _file(name).name == name


def test_empty_content_is_a_valid_zero_byte_file() -> None:
    event = parse_event(_raw([{"name": "empty", "content": ""}]))

    assert isinstance(event, Succeeded)
    assert event.files == (_file("empty", b""),)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("bGEgbGE", id="missing padding"),
        pytest.param("bGEgbGE==", id="bad padding"),
        pytest.param("-_8=", id="URL-safe alphabet"),
        pytest.param("bGEg\nbGE=", id="embedded newline"),
        pytest.param(" bGEgbGE=", id="leading space"),
        pytest.param("QR==", id="non-zero pad bits"),
        pytest.param("bGEgbGÉ=", id="non-ASCII"),
        pytest.param(5, id="not a string"),
    ],
)
def test_content_that_is_not_canonical_base64_is_refused(content: object) -> None:
    assert _violation(_raw([{"name": "a", "content": content}])).kind is (
        ViolationKind.SCHEMA_MISMATCH
    )


@pytest.mark.parametrize("size_over_ceiling", [0, 1])
def test_all_contents_together_may_hold_exactly_the_ceiling(size_over_ceiling: int) -> None:
    half = MAX_RUN_FILES_BYTES // 2
    files = (
        _file("a", bytes(half)),
        _file("b", bytes(MAX_RUN_FILES_BYTES - half + size_over_ceiling)),
    )

    if size_over_ceiling:
        with pytest.raises(ValidationError, match="together exceed"):
            succeeded(5, "Done.", files)
    else:
        assert succeeded(5, "Done.", files).files == files


def test_a_malformed_content_never_appears_in_a_violation() -> None:
    marker_content = SECRET_MARKER + "=" * 5

    detail = _violation(_raw([{"name": "a", "content": marker_content}])).detail

    assert SECRET_MARKER not in detail


def test_a_clash_between_files_never_puts_their_content_in_a_violation() -> None:
    encoded_marker = base64.b64encode(SECRET_MARKER.encode()).decode()
    twin = {"name": "a", "content": encoded_marker}

    detail = _violation(_raw([twin, twin])).detail

    assert "unique" in detail
    assert SECRET_MARKER not in detail
    assert encoded_marker not in detail
