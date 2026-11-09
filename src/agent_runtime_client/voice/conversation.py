"""One running conversation of a voice loop: its decided pairs and the lines still undecided.

It lives in memory while the loop runs and is stored nowhere. A decided pair is the user message
of a decision's lines and what the agent played, or the silent reply; what the model wrote and
did not play is never recorded.
"""

from __future__ import annotations

import json

from agent_runtime_client.member import PlaceEntry
from agent_runtime_client.turn import TurnMessage, TurnRole


class Conversation:
    def __init__(self) -> None:
        self._decided: list[TurnMessage] = []
        self._undecided: list[str] = []
        self._deciding: list[str] = []
        self._last_seq: int | None = None

    def hear(self, line: PlaceEntry) -> None:
        """Enter a line in ``seq`` order; the agent's own spoken line is skipped.

        The loop records what it played itself. A line after a gap in ``seq`` carries
        ``missed_before``, the number of lines not heard. Raises ``ValueError`` when ``seq`` is
        not above the last one.
        """
        if self._last_seq is not None and line.seq <= self._last_seq:
            raise ValueError(f"seq {line.seq} is not above the last seq {self._last_seq}")
        missed = 0 if self._last_seq is None else line.seq - self._last_seq - 1
        self._last_seq = line.seq
        if line.own and line.spoken:
            return
        self._undecided.append(_written(line, missed))

    def open_decision(self) -> list[TurnMessage]:
        """The messages of a decision over every undecided line, ending with them."""
        self._deciding, self._undecided = self._undecided, []
        return [*self._decided, _user_message(self._deciding)]

    def close_decision(self, reply: str | None) -> None:
        """Record the open decision's lines with ``reply``.

        With ``None`` they stay undecided, ahead of the lines heard since.
        """
        if reply is None:
            self._undecided = [*self._deciding, *self._undecided]
        else:
            self._decided += [
                _user_message(self._deciding),
                TurnMessage(role=TurnRole.ASSISTANT, text=reply),
            ]
        self._deciding = []


def _written(line: PlaceEntry, missed: int) -> str:
    fields: dict[str, object] = {
        "name": line.name,
        "kind": line.kind,
        "own": line.own,
        "spoken": line.spoken,
        "text": line.text,
    }
    if missed:
        fields["missed_before"] = missed
    return json.dumps(fields, ensure_ascii=False)


def _user_message(lines: list[str]) -> TurnMessage:
    return TurnMessage(role=TurnRole.USER, text="\n".join(lines))
