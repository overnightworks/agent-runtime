"""What an app's MCP endpoint keeps out of the SDK: its path, the revision, the strict reader.

The person front speaks revision ``2026-07-28`` of the Model Context Protocol through the
official SDK, which lives behind the ``mcp-server`` extra. This base module holds only what the
SDK does not own and imports no extra: the constants a host uses, the JSON reader stricter than
the SDK's, and the check for a bearer reflected into an answer. It opens no socket and reads no
clock or environment.
"""

from __future__ import annotations

import json
import math
from typing import Any, Final

from agents.errors import raise_without_chain
from agents.turn import refuse_non_json_constant

JSON_MEDIA_TYPE: Final = "application/json"
MCP_ENDPOINT_PATH: Final = "/mcp"
PROTOCOL_VERSION_HEADER: Final = "MCP-Protocol-Version"
PROTOCOL_VERSION: Final = "2026-07-28"


def _object_without_duplicate_members(members: list[tuple[str, Any]]) -> dict[str, Any]:
    document = dict(members)
    if len(document) != len(members):
        raise ValueError("an object repeats a member")
    return document


def _finite_number(literal: str) -> float:
    number = float(literal)
    if not math.isfinite(number):
        raise ValueError("a number overflows")
    return number


def read_strict_json(body: bytes) -> Any:
    """The one strict JSON reader: no repeated member, no ``NaN``, no number that overflows.

    The only exception that leaves is ``ValueError``; nesting past the recursion limit is
    converted without chain. The caller owns any size rule.
    """
    try:
        return json.loads(
            body.decode("utf-8"),
            parse_constant=refuse_non_json_constant,
            parse_float=_finite_number,
            object_pairs_hook=_object_without_duplicate_members,
        )
    except RecursionError:
        raise_without_chain(ValueError("the document nests too deeply"))


def carries_signature_of(token: str, data: bytes) -> bool:
    """Whether ``data`` carries ``token``, whole or as its signature segment.

    One segment is enough: every whole token contains its signature segment, the part after the
    last ``.``. The person front's bodies are JSON and tool results are asked after decoding, so
    the segment is asked verbatim and in the two ways a JSON writer spells it: base64url
    characters and ``.`` stay as they are, anything else may be ``\\u``-escaped or written in
    UTF-8. A token split across strings, base64-encoded or reversed is not detected: this stops
    reflection, not an app that encodes its own bearer.
    """
    signature_segment = token.rsplit(".", 1)[-1]
    json_spellings = {
        json.dumps(signature_segment, ensure_ascii=ascii_only)[1:-1] for ascii_only in (True, False)
    }
    return any(
        spelling.encode("utf-8") in data for spelling in {signature_segment, *json_spellings}
    )
