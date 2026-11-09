"""The echo rule: the agent's own answer, heard back through a person's microphone."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from agents.member import PlaceEntry


def is_echo(line: PlaceEntry, answer: Sequence[str]) -> bool:
    """Whether ``line`` repeats the sentences of the agent's latest ``answer``.

    Only a spoken line that is not the agent's own can be an echo: a person who types the
    agent's words did not hear them. It is one when it has counted words and at least half of
    them are among the counted words of ``answer``. A counted word is one split at whitespace,
    stripped of the punctuation at its edges and still longer than two characters, compared in
    lower case; so a short reply such as "Ja, ok." never counts, and a word that is only
    punctuation counts on neither side.
    """
    if line.own or not line.spoken:
        return False
    counted = _counted_words(line.text)
    answered = _counted_words(" ".join(answer))
    return bool(counted) and 2 * len(counted & answered) >= len(counted)


def _counted_words(text: str) -> frozenset[str]:
    return frozenset(word for word in map(_bare, text.lower().split()) if len(word) > 2)


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
