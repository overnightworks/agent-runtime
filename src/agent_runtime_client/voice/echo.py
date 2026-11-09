"""The echo rule: the agent's own answer, heard back through a person's microphone."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from agent_runtime_client.member import PlaceEntry


def is_echo(line: PlaceEntry, answer: Sequence[str]) -> bool:
    """Whether ``line`` repeats the sentences of the agent's latest ``answer``.

    Only a spoken line that is not the agent's own can be an echo: a person who types the
    agent's words did not hear them. It is one when it has words longer than two characters,
    split at whitespace, and at least half of these are words of ``answer``, compared in lower
    case without the punctuation at their edges. A word's length counts its punctuation, so
    removing it only ever finds more of a line's words in the answer.
    """
    if line.own or not line.spoken:
        return False
    words = _long_words(line.text)
    answered = {_bare(word) for sentence in answer for word in _long_words(sentence)}
    repeated = sum(_bare(word) in answered for word in words)
    return bool(words) and 2 * repeated >= len(words)


def _long_words(text: str) -> frozenset[str]:
    return frozenset(word for word in text.lower().split() if len(word) > 2)


def _bare(word: str) -> str:
    """The word without the punctuation at its edges."""
    start, end = 0, len(word)
    while start < end and _is_punctuation(word[start]):
        start += 1
    while end > start and _is_punctuation(word[end - 1]):
        end -= 1
    return word[start:end]


def _is_punctuation(character: str) -> bool:
    return unicodedata.category(character).startswith("P")
