"""Sentence boundaries of speech text."""

import pytest

from agents.voice.sentences import split_sentences


@pytest.mark.parametrize(
    ("text", "sentences"),
    [
        ("Das ist Dr. Müller. Er kommt.", ("Das ist Dr. Müller.", "Er kommt.")),
        ("Er kommt z. B. morgen. Ja.", ("Er kommt z. B. morgen.", "Ja.")),
        ("Am 3. Mai kommt er. Ja.", ("Am 3. Mai kommt er.", "Ja.")),
        ("Er wurde 3. Dann kam er.", ("Er wurde 3. Dann kam er.",)),
        ("Gut! Wirklich? Ja.", ("Gut!", "Wirklich?", "Ja.")),
        ("Hallo. Wie geht", ("Hallo.", "Wie geht")),
        (
            "We meet at 10 a.m. in the hall. Bring Dr. Smith!",
            ("We meet at 10 a.m. in the hall.", "Bring Dr. Smith!"),
        ),
        ("", ()),
    ],
)
def test_text_splits_into_sentences_and_an_ordinal_does_not_end_one(text, sentences):
    assert split_sentences(text) == sentences
