"""A reply is read round by round: its opening marker first, then its sentences as they complete."""

import json
import random

import pytest

from agents.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agents.voice import ReplyUnreadable, StayedSilent
from agents.voice.conversation import Conversation
from agents.voice.reply import (
    PROTOCOL_TEXT,
    SILENT_REPLY,
    ReplyReader,
    SentenceRelease,
)
from agents.voice.sentences import split_sentences
from tests.support.voice_lines import STREAMED_ANSWERS, a_line, random_pieces

SILENT = (StayedSilent(),)
UNREADABLE = (ReplyUnreadable(),)


def pieces(*texts: str) -> list:
    return [AssistantTextEvent(text=text) for text in texts]


def a_final(*texts: str) -> list:
    return [*pieces(*texts), FinalEvent(text="".join(texts))]


def a_tool_call() -> list:
    return [
        ToolCallEvent(tool_use_id="call-1", name="look_up", input={}),
        ToolResultEvent(tool_use_id="call-1", content="found"),
    ]


def read_turn(events: list) -> tuple[tuple, list[str]]:
    """The openings the reader reported and the sentences it released, over a whole turn."""
    reader = ReplyReader()
    openings, released = [], []
    for event in events:
        reading = reader.read(event)
        released += reading.sentences
        if reading.opening is not None:
            openings.append(reading.opening)
    return tuple(openings), released


def released_by(release: SentenceRelease, *texts: str) -> list[str]:
    return [sentence for text in texts for sentence in release.feed(text)] + [*release.flush()]


@pytest.mark.parametrize(
    ("events", "openings", "spoken"),
    [
        (a_final("  [ Silent ] Nichts zu sagen."), SILENT, []),
        (a_final("[SILENT]"), SILENT, []),
        (a_final("\n[sil", "ent]", " Auch das nicht."), SILENT, []),
        (a_final("[", " " * 5 + "silent" + " " * 4 + "]"), SILENT, []),
        (a_final("[shout] Hallo."), UNREADABLE, []),
        (a_final("[", " " * 5 + "silent" + " " * 5 + "] Hallo."), UNREADABLE, []),
        (a_final("[" + " " * 5 + "silent" + " " * 4, "]"), SILENT, []),
        (a_final("[\tsilent] Hallo."), UNREADABLE, []),
        (a_final("[" + "x" * 16, "] Hallo."), UNREADABLE, []),
        (a_final("[sile"), UNREADABLE, []),
        (a_final("Ja [silent] gut."), (), ["Ja [silent] gut."]),
        (a_final("Gut. ", "[silent]"), (), ["Gut.", "[silent]"]),
        (a_final("  ", " Gut. Danke."), (), ["Gut.", "Danke."]),
        (a_final("Das habe ich vergessen."), (), ["Das habe ich vergessen."]),
    ],
)
def test_a_rounds_opening_marker_decides_whether_any_of_it_is_spoken(events, openings, spoken):
    assert read_turn(events) == (openings, spoken)


@pytest.mark.parametrize(
    ("events", "openings", "spoken"),
    [
        (
            [*pieces("Einen Moment. Ich schaue"), *a_tool_call(), *a_final("[silent]")],
            SILENT,
            ["Einen Moment.", "Ich schaue"],
        ),
        ([*pieces("[silent]"), *a_tool_call(), *a_final("Gefunden.")], SILENT, ["Gefunden."]),
        (
            [*pieces("[sil"), *a_tool_call(), *a_final("ent] Gefunden.")],
            UNREADABLE,
            ["ent] Gefunden."],
        ),
        ([*a_tool_call(), *a_final("Gefunden.")], (), ["Gefunden."]),
    ],
)
def test_a_tool_call_ends_a_round_and_the_next_round_is_read_afresh(events, openings, spoken):
    assert read_turn(events) == (openings, spoken)


def test_the_protocol_text_names_the_marker_the_reader_takes_as_silence():
    assert SILENT_REPLY in PROTOCOL_TEXT
    assert read_turn(a_final(SILENT_REPLY)) == (SILENT, [])


def test_the_protocol_text_names_every_key_a_written_line_carries():
    conversation = Conversation(window_bytes=4096)
    conversation.hear(a_line(1, "Eins."))
    conversation.hear(a_line(3, "Drei."))
    conversation.hear(a_line(5, "Ada legt ab.", kind="move", spoken=False, act=True))

    written = conversation.open_decision()[-1].text.splitlines()

    keys = {key for line in written for key in json.loads(line)}
    assert {key for key in keys if f'"{key}"' not in PROTOCOL_TEXT} == set()


def test_the_protocol_text_names_the_note_that_opens_a_window_with_earlier_parts_dropped():
    conversation = Conversation(window_bytes=1)
    conversation.hear(a_line(1, "Eins."))
    conversation.open_decision()
    conversation.close_decision("Gut.")
    conversation.hear(a_line(2, "Zwei."))

    opening = conversation.open_decision()[0].text.splitlines()[0]

    assert opening in PROTOCOL_TEXT


@pytest.mark.parametrize(
    "steps",
    [
        [("Hallo Welt.", ()), (" Wie", ("Hallo Welt.",))],
        [("Am 3. M", ()), ("ai kommt er. Ja", ("Am 3. Mai kommt er.",))],
        [("Das ist Dr.", ()), (" Müller. Er", ("Das ist Dr. Müller.",))],
        [("Am 3.\nMai kommt er. Ja", ("Am 3.\nMai kommt er.",))],
        [("Am 3.  Mai kommt er. Ja", ("Am 3.  Mai kommt er.",))],
        [("Am 3.\n\nMai kommt er. Ja", ("Am 3.\n\nMai kommt er.",))],
    ],
)
def test_a_sentence_is_released_only_once_content_follows_it(steps):
    release = SentenceRelease(split_sentences)
    assert [release.feed(text) for text, _ in steps] == [released for _, released in steps]


@pytest.mark.parametrize(
    ("texts", "sentences"),
    [
        (("Eins. ", "Zwei! ", "Drei?"), ["Eins.", "Zwei!", "Drei?"]),
        (("Eins. Zw", "ei. Drei"), ["Eins.", "Zwei.", "Drei"]),
        (("Ohne Ende",), ["Ohne Ende"]),
        ((), []),
        (("",), []),
        (("Eins. Zwei ", "  "), ["Eins.", "Zwei"]),
        (("  ",), []),
    ],
)
def test_pieces_release_whole_sentences_and_the_flush_releases_the_rest(texts, sentences):
    assert released_by(SentenceRelease(split_sentences), *texts) == sentences


def test_a_flush_empties_the_release():
    release = SentenceRelease(split_sentences)
    release.feed("Rest")
    release.flush()

    assert release.flush() == ()


@pytest.mark.parametrize("line", ["- Punkt eins ohne Ende\n", "am 3. ", "wort "])
def test_an_unterminated_stream_hands_the_splitter_linear_text(line):
    handed_over = 0

    def counting_split(text: str) -> tuple[str, ...]:
        nonlocal handed_over
        handed_over += len(text)
        return split_sentences(text)

    release = SentenceRelease(counting_split)
    text = (line * 1000)[:10_000]
    for character in text:
        release.feed(character)

    assert handed_over < 100 * len(text)


@pytest.mark.parametrize(("text", "sentences"), STREAMED_ANSWERS)
def test_an_answer_cut_anywhere_releases_each_sentence_once_whole_and_after_content(
    text, sentences
):
    chooser = random.Random(text)  # noqa: S311 - seeded cut points that repeat on every run
    for _ in range(40):
        reader = ReplyReader()
        released: list[str] = []
        written = ""
        position = 0
        for piece in random_pieces(text, chooser):
            written += piece
            for sentence in reader.read(AssistantTextEvent(text=piece)).sentences:
                position = written.index(sentence, position) + len(sentence)
                assert written[position:].strip(), (sentence, written)
                released.append(sentence)
        released += reader.read(FinalEvent(text=text)).sentences

        assert released == sentences
