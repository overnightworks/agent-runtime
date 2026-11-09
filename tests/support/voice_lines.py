"""The lines a voice test hears and the answers it streams, as plain data with no adapter."""

from __future__ import annotations

import random

from agent_runtime_client.member import PlaceEntry


def a_line(
    seq: int, text: str, *, name: str = "Ada", kind: str = "person", own=False, spoken=True
) -> PlaceEntry:
    return PlaceEntry(seq=seq, kind=kind, name=name, text=text, own=own, spoken=spoken)


STREAMED_ANSWERS = [
    (
        "Am 3. Mai beginnt die Konferenz in Berlin. Sie dauert drei Tage! "
        "Wer von euch kommt mit? Dr. Müller hält z. B. den ersten Vortrag.",
        [
            "Am 3. Mai beginnt die Konferenz in Berlin.",
            "Sie dauert drei Tage!",
            "Wer von euch kommt mit?",
            "Dr. Müller hält z. B. den ersten Vortrag.",
        ],
    ),
    (
        # A closing German quote ends no sentence here: it is released with the next one.
        "Er sagte: „Das geht nie.“ Doch er kam. Dann ging er wieder.",
        ["Er sagte: „Das geht nie.“ Doch er kam.", "Dann ging er wieder."],
    ),
    (
        "We meet at 10 a.m. in the hall. Bring Dr. Smith along! "
        "Does everyone have the slides? Good.",
        [
            "We meet at 10 a.m. in the hall.",
            "Bring Dr. Smith along!",
            "Does everyone have the slides?",
            "Good.",
        ],
    ),
    (
        "Erstens die Zahlen.\nZweitens der Plan. Um 9.30 Uhr geht es los.",
        ["Erstens die Zahlen.", "Zweitens der Plan.", "Um 9.30 Uhr geht es los."],
    ),
    (
        # Longer than the words the release re-judges; the end after its abbreviation needs them.
        "Zur Sitzung am Montag kommen wie immer die Kollegen aus Hamburg, Bremen und Berlin u. ä. "
        "Danach gehen wir essen. Gut so.",
        [
            "Zur Sitzung am Montag kommen wie immer die Kollegen aus Hamburg, Bremen und Berlin "
            "u. ä.",
            "Danach gehen wir essen.",
            "Gut so.",
        ],
    ),
]


def random_pieces(text: str, chooser: random.Random) -> list[str]:
    cuts = sorted(chooser.sample(range(1, len(text)), chooser.randint(1, len(text) // 3)))
    return [text[start:end] for start, end in zip([0, *cuts], [*cuts, len(text)], strict=True)]
