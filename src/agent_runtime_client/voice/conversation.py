"""One running conversation of a voice loop: its decided pairs and the lines still undecided.

It lives in memory while the loop runs and is stored nowhere. A decided pair is the user message
of a decision's lines and what the agent played, or the silent reply; what the model wrote and
did not play is never recorded. A decision sends at most ``window_bytes`` of it, counted as the
UTF-8 bytes of the messages' texts, because a provider's tokens cannot be counted here; only the
newest line goes beyond it, alone and behind ``WINDOW_NOTE`` once anything went. What a decision
dropped never returns.
"""

from __future__ import annotations

import json
from typing import Final

from agent_runtime_client.member import PlaceEntry
from agent_runtime_client.turn import TurnMessage, TurnRole

WINDOW_NOTE: Final = "(Earlier parts of this conversation are no longer shown.)"
"""The line that opens the first user message once the window has dropped earlier parts."""

_LINE_BREAK: Final = "\n"


class Conversation:
    def __init__(self, window_bytes: int) -> None:
        self._window_bytes = window_bytes
        self._decided: list[TurnMessage] = []
        self._undecided: list[str] = []
        self._deciding: list[str] = []
        self._last_seq: int | None = None
        self._missed = 0
        self._noted = False

    def hear(self, line: PlaceEntry) -> None:
        """Enter a line in ``seq`` order; the agent's own spoken line is skipped.

        The loop records what it played itself. The next line that enters after a gap in ``seq``
        carries ``missed_before``, the number of lines not heard. Raises ``ValueError`` when
        ``seq`` is not above the last one.
        """
        self.skip(line)
        if line.own and line.spoken:
            return
        self._undecided.append(_written(line, self._missed))
        self._missed = 0

    def skip(self, line: PlaceEntry) -> None:
        """Take ``line``'s place in ``seq`` order without entering it.

        A skipped line is never counted as missed, and a gap before it passes on to the next
        line that enters. Raises ``ValueError`` when ``seq`` is not above the last one.
        """
        if self._last_seq is not None and line.seq <= self._last_seq:
            raise ValueError(f"seq {line.seq} is not above the last seq {self._last_seq}")
        if self._last_seq is not None:
            self._missed += line.seq - self._last_seq - 1
        self._last_seq = line.seq

    def open_decision(self) -> list[TurnMessage]:
        """The messages of a decision over every undecided line, ending with them.

        Beyond the window, the oldest decided pairs go until a quarter of it is free, then the
        oldest of these lines while they alone exceed it; the newest line always stays. Once
        anything went, ``WINDOW_NOTE`` opens the first user message.
        """
        self._deciding, self._undecided = self._undecided, []
        if self._size() > self._window_bytes:
            self._drop_the_oldest()
        messages = [*self._decided, _user_message(self._deciding)]
        if self._noted:
            messages[0] = _user_message([WINDOW_NOTE, messages[0].text])
        return messages

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

    def _drop_the_oldest(self) -> None:
        if not self._decided and len(self._deciding) <= 1:
            return
        self._noted = True
        size = self._size()
        while self._decided and size > self._window_bytes * 3 / 4:
            user, assistant = self._decided[:2]
            del self._decided[:2]
            size -= _bytes_of(user.text) + _bytes_of(assistant.text)
        while len(self._deciding) > 1 and size > self._window_bytes:
            size -= _bytes_of(self._deciding.pop(0) + _LINE_BREAK)

    def _size(self) -> int:
        """The UTF-8 bytes of the open decision's messages' texts, the note's line included."""
        note = _bytes_of(WINDOW_NOTE + _LINE_BREAK) if self._noted else 0
        decided = sum(_bytes_of(message.text) for message in self._decided)
        return note + decided + _bytes_of(_LINE_BREAK.join(self._deciding))


def _bytes_of(text: str) -> int:
    return len(text.encode())


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
    return TurnMessage(role=TurnRole.USER, text=_LINE_BREAK.join(lines))
