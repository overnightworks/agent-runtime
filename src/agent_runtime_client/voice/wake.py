"""The standard wake rule: a cheap local check of which heard lines cost a decision."""

from __future__ import annotations

import re
from typing import Final

from agent_runtime_client.member import PlaceEntry
from agent_runtime_client.voice import WakeRule

_CLOSING_QUOTES: Final = "\"'”’“‘»«"


def addressed_or_asked(names: frozenset[str]) -> WakeRule:
    """Wake on a line that names the agent, asks a question or is the first after its answer.

    A line is named when one of ``names`` stands in it as a whole word, in any case, and asks
    when it ends with "?" before its trailing spaces and closing quotes. Another agent's line
    wakes by the same rule. A line that does not wake still enters the conversation.
    """
    if not names or "" in names:
        raise ValueError("the wake rule needs at least one name, and none of them empty")
    alternatives = "|".join(re.escape(name.casefold()) for name in names)
    named = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")

    def wakes(line: PlaceEntry, *, first_after_answer: bool) -> bool:
        return (
            first_after_answer
            or named.search(line.text.casefold()) is not None
            or line.text.rstrip().rstrip(_CLOSING_QUOTES).endswith("?")
        )

    return wakes
