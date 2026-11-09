"""The echo rule: the agent's latest answer, heard back through a microphone, never enters."""

import pytest

from agent_runtime_client.backend.fake_gateway import FakeGateway, completion
from agent_runtime_client.voice import (
    EchoDropped,
    SentenceRefused,
    SentenceStarted,
    VoiceRefused,
    Woke,
)
from agent_runtime_client.voice.echo import is_echo
from agent_runtime_client.voice.wake import addressed_or_asked
from tests.support import voice_support
from tests.support.voice_lines import a_line
from tests.support.voice_support import (
    A_GATEWAY_REFUSAL,
    RETURNED,
    PacedGateway,
    RecordingVoice,
    finish,
    hang,
    messages_of,
    piece,
    rows_of,
)

place = voice_support.place

COUNTED_WORDS = [
    pytest.param("Die Folie zeigt den Umsatz", [], False, id="no answer yet"),
    pytest.param(
        "Die Folie zeigt den Umsatz", ["Die Folie zeigt den Umsatz"], True, id="the answer again"
    ),
    pytest.param(
        "Folie Umsatz Kaffee Kuchen", ["Die Folie zeigt den Umsatz"], True, id="exactly half"
    ),
    pytest.param(
        "Folie Umsatz Kaffee Kuchen Tee", ["Die Folie zeigt den Umsatz"], False, id="below half"
    ),
    pytest.param("ja da so", ["ja da so"], False, id="only words of two letters or fewer"),
    pytest.param("ja so Folie Kaffee", ["Die Folie"], True, id="short words do not count"),
    pytest.param(
        "Folie Folie Folie Kaffee Kuchen", ["Die Folie"], False, id="a repeated word counts once"
    ),
    pytest.param("Umsatzzahlen steigen", ["Der Umsatz steigt"], False, id="part of a word"),
    pytest.param(
        "Folie Kosten Kaffee",
        ["Die Folie zeigt den Umsatz", "Danach kommen die Kosten"],
        True,
        id="every sentence of the answer counts",
    ),
    pytest.param(
        "DIE FOLIE ZEIGT DEN UMSATZ", ["Die Folie zeigt den Umsatz"], True, id="another case"
    ),
    pytest.param(
        "Folie\nzeigt\tUmsatz", ["Die Folie zeigt den Umsatz"], True, id="any whitespace splits"
    ),
    pytest.param("geht's", ["gehts"], False, id="punctuation inside a word stays"),
    pytest.param(
        "---", ["Warte ... gleich."], False, id="a word that is only punctuation counts nowhere"
    ),
    pytest.param(
        "Ja, ok.",
        ["Ist das so ok für dich?"],
        False,
        id="a short reply to the answer's question",
    ),
    pytest.param("So, ok.", ["Ist das so ok für dich?"], False, id="another short reply"),
    pytest.param("Ja, ja.", ["Da hast du ja recht."], False, id="a short word said twice"),
    pytest.param(
        "Ja, ok.", ["Ist das ok?"], False, id="a short word with its punctuation is still short"
    ),
    pytest.param(
        "Genau, die Folie zeigt den Umsatz",
        ["Die Folie zeigt den Umsatz"],
        True,
        id="a person repeating the answer",
    ),
    pytest.param(
        "Die Folie zeigt den Umsatz und ich ergänze die Kosten der Werbung",
        ["Die Folie zeigt den Umsatz"],
        False,
        id="an echo mixed with more words of a person",
    ),
]

# Cases that words split at whitespace alone, punctuation kept, would decide the other way.
BARE_WORDS = [
    pytest.param("Danke schön", ["Danke, schön."], True, id="a comma and a period"),
    pytest.param("Umsatz? Kosten!", ["Der Umsatz, die Kosten."], True, id="other punctuation"),
    pytest.param("„Gern geschehen…“", ["Gern geschehen."], True, id="quotes and an ellipsis"),
    pytest.param("DANKE SCHÖN!", ["Danke, schön."], True, id="case and punctuation together"),
    pytest.param("Gern.", ["gern"], True, id="a period after a long word"),
    pytest.param(
        "Ja, genau", ["Ja, gerne"], False, id="a short word carrying the same comma counts nowhere"
    ),
    pytest.param("--- Folie", ["--- Kaffee"], False, id="the same bare punctuation counts nowhere"),
    pytest.param(
        "Ja, ok, Folie",
        ["Die Folie"],
        True,
        id="a short word with punctuation does not count in the line",
    ),
    pytest.param(
        "--- Folie Torte",
        ["Folie"],
        True,
        id="a word that is only punctuation does not count in the line",
    ),
]


@pytest.mark.parametrize(("text", "answer", "expected"), COUNTED_WORDS)
def test_a_spoken_line_is_an_echo_when_half_its_long_words_are_words_of_the_answer(
    text, answer, expected
):
    assert is_echo(a_line(1, text), answer) is expected


@pytest.mark.parametrize(("text", "answer", "expected"), BARE_WORDS)
def test_a_word_counts_and_compares_without_the_punctuation_at_its_edges(text, answer, expected):
    assert is_echo(a_line(1, text), answer) is expected


@pytest.mark.parametrize(
    ("speaker", "expected"),
    [
        pytest.param({"spoken": True}, True, id="a person's spoken line"),
        pytest.param({"spoken": False}, False, id="a person's written line"),
        pytest.param(
            {"name": "listener", "kind": "agent", "own": True, "spoken": True},
            False,
            id="the agent's own spoken line",
        ),
        pytest.param(
            {"name": "listener", "kind": "agent", "own": True, "spoken": False},
            False,
            id="the agent's own written line",
        ),
    ],
)
def test_only_a_spoken_line_that_is_not_the_agents_own_can_be_an_echo(speaker, expected):
    line = a_line(1, "Die Folie zeigt den Umsatz", **speaker)

    assert is_echo(line, ["Die Folie zeigt den Umsatz."]) is expected


QUESTION = "Wie lief das Quartal?"
FIRST = "Die Folie zeigt den Umsatz."
SECOND = "Danach kommen die Kosten."
FIRST_HEARD_BACK = "die Folie zeigt den Umsatz"
SECOND_HEARD_BACK = "danach kommen die Kosten"


def rows_sent(request) -> list[dict]:
    """Every line a request sent, in order."""
    return [
        row
        for message in messages_of(request)[1:]
        if message["role"] == "user"
        for row in rows_of(message)
    ]


def lines_sent(request) -> list[str]:
    """The text of every line a request sent, in order."""
    return [row["text"] for row in rows_sent(request)]


async def while_its_first_sentence_plays(trace, voice) -> None:
    await trace.until(lambda: ("play", FIRST) in trace.entries)


async def after_it_ended(trace, voice) -> None:
    await while_its_first_sentence_plays(trace, voice)
    voice.release(FIRST)
    await trace.settled()


@pytest.mark.parametrize(
    "moment",
    [
        pytest.param(while_its_first_sentence_plays, id="while its first sentence plays"),
        pytest.param(after_it_ended, id="after it ended"),
    ],
)
def test_a_spoken_line_repeating_the_answer_is_dropped_never_asked_and_never_sent(place, moment):
    voice = RecordingVoice(place.trace, held={FIRST})
    gateway = FakeGateway(completion(f"{FIRST} {SECOND}"), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await moment(trace, voice)
        yield a_line(2, FIRST_HEARD_BACK)
        voice.release(FIRST)
        await trace.settled()
        yield a_line(3, "Danke.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert EchoDropped(FIRST_HEARD_BACK) in place.trace.events
    assert place.trace.asked == [(QUESTION, False), ("Danke.", True)]
    assert lines_sent(gateway.requests[-1]) == [QUESTION, "Danke."]


def test_an_echo_heard_while_the_next_decision_is_read_repeats_the_answer_before_it(place):
    trace = place.trace
    gateway = PacedGateway(
        trace, [piece(f"{FIRST} {SECOND}"), finish()], [trace.settled, piece("Gern."), finish()]
    )

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.settled()
        yield a_line(2, "Und weiter?")
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield a_line(3, FIRST_HEARD_BACK)
        await trace.until(lambda: "Gern." in trace.played)
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert trace.events == [
        Woke(),
        SentenceStarted(FIRST),
        SentenceStarted(SECOND),
        Woke(),
        EchoDropped(FIRST_HEARD_BACK),
        SentenceStarted("Gern."),
    ]
    assert trace.asked == [(QUESTION, False), ("Und weiter?", True)]


def test_a_line_repeating_only_an_older_answer_enters_while_the_latest_one_plays(place):
    voice = RecordingVoice(place.trace, held={"Gern geschehen."})
    gateway = FakeGateway(
        completion(FIRST), completion("Gern geschehen. Bis gleich."), completion("Ja.")
    )

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.settled()
        yield a_line(2, "Danke.")
        await trace.until(lambda: ("play", "Gern geschehen.") in trace.entries)
        yield a_line(3, FIRST_HEARD_BACK)
        yield a_line(4, "gern geschehen")
        voice.release("Gern geschehen.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.asked == [(QUESTION, False), ("Danke.", True), (FIRST_HEARD_BACK, False)]
    assert EchoDropped("gern geschehen") in place.trace.events
    assert EchoDropped(FIRST_HEARD_BACK) not in place.trace.events
    assert lines_sent(gateway.requests[-1]) == [QUESTION, "Danke.", FIRST_HEARD_BACK]


def test_a_line_repeating_only_an_older_answer_enters_while_the_next_decision_is_read(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [piece(FIRST), finish()],
        [piece("Gern geschehen."), finish()],
        [trace.settled, piece("Ja."), finish()],
    )

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.settled()
        yield a_line(2, "Danke.")
        await trace.settled()
        yield a_line(3, "Und weiter?")
        await trace.until(lambda: trace.count(Woke()) == 3)
        yield a_line(4, FIRST_HEARD_BACK)
        await trace.until(lambda: "Ja." in trace.played)
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert EchoDropped(FIRST_HEARD_BACK) not in trace.events
    assert (FIRST_HEARD_BACK, False) in trace.asked


def test_a_written_line_with_the_answers_words_enters_and_is_asked(place):
    gateway = FakeGateway(completion(FIRST), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.settled()
        yield a_line(2, FIRST_HEARD_BACK)
        yield a_line(3, FIRST_HEARD_BACK, spoken=False)
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert place.trace.events.count(EchoDropped(FIRST_HEARD_BACK)) == 1
    assert place.trace.asked == [(QUESTION, False), (FIRST_HEARD_BACK, True)]
    assert lines_sent(gateway.requests[-1]) == [QUESTION, FIRST_HEARD_BACK]


def test_a_short_spoken_reply_to_the_agents_question_wakes_it_as_the_first_line_after(place):
    asking = "Ist das so ok für dich?"
    gateway = FakeGateway(completion(asking), completion("Gut."))

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.settled()
        yield a_line(2, "Ja, ok.")
        await trace.settled()

    assert (
        place.run(hearing, gateway, wakes=addressed_or_asked(frozenset({"Listener"}))) == RETURNED
    )

    assert EchoDropped("Ja, ok.") not in place.trace.events
    assert place.trace.asked == [(QUESTION, False), ("Ja, ok.", True)]
    assert lines_sent(gateway.requests[-1]) == [QUESTION, "Ja, ok."]


def test_a_sentence_still_queued_does_not_count_while_the_one_before_it_plays(place):
    voice = RecordingVoice(place.trace, held={FIRST})
    gateway = FakeGateway(completion(f"{FIRST} {SECOND}"), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await while_its_first_sentence_plays(trace, voice)
        await trace.settled()
        yield a_line(2, SECOND_HEARD_BACK)
        yield a_line(3, FIRST_HEARD_BACK)
        voice.release(FIRST)
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.asked == [(QUESTION, False), (SECOND_HEARD_BACK, False)]
    assert EchoDropped(FIRST_HEARD_BACK) in place.trace.events
    assert lines_sent(gateway.requests[-1]) == [QUESTION, SECOND_HEARD_BACK]


def test_a_sentence_still_rendering_does_not_count_and_the_one_played_before_it_does(place):
    voice = RecordingVoice(place.trace, hangs={("render", SECOND)})
    gateway = FakeGateway(completion(f"{FIRST} {SECOND}"), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.until(lambda: ("render", SECOND) in trace.entries)
        await trace.settled()
        yield a_line(2, SECOND_HEARD_BACK)
        yield a_line(3, FIRST_HEARD_BACK)
        await trace.settled()
        await trace.host_cancels()

    assert place.run(hearing, gateway, voice=voice).cancelled

    assert place.trace.asked == [(QUESTION, False), (SECOND_HEARD_BACK, False)]
    assert EchoDropped(FIRST_HEARD_BACK) in place.trace.events


@pytest.mark.parametrize("call", ["render", "play"])
def test_a_refused_sentence_does_not_count_and_the_one_played_before_it_does(place, call):
    voice = RecordingVoice(place.trace, raises={(call, SECOND): VoiceRefused()})
    gateway = FakeGateway(completion(f"{FIRST} {SECOND}"), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.settled()
        yield a_line(2, SECOND_HEARD_BACK)
        yield a_line(3, FIRST_HEARD_BACK)
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert SentenceRefused(SECOND) in place.trace.events
    assert place.trace.asked == [(QUESTION, False), (SECOND_HEARD_BACK, True)]
    assert EchoDropped(FIRST_HEARD_BACK) in place.trace.events


@pytest.mark.parametrize(
    ("reply", "voice_raises"),
    [
        pytest.param(completion("[silent]"), {}, id="it stayed silent"),
        pytest.param(A_GATEWAY_REFUSAL, {}, id="its turn failed"),
        pytest.param(
            completion("Gern geschehen."),
            {("render", "Gern geschehen."): VoiceRefused()},
            id="its sentence was refused before it started",
        ),
        pytest.param(
            completion("Gern geschehen."),
            {("play", "Gern geschehen."): VoiceRefused()},
            id="its sentence was refused while it played",
        ),
    ],
)
def test_after_a_decision_that_played_nothing_the_answer_before_it_is_still_the_latest(
    place, reply, voice_raises
):
    voice = RecordingVoice(place.trace, raises=voice_raises)
    gateway = FakeGateway(completion(FIRST), reply, completion("Ja."))

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.settled()
        yield a_line(2, "Danke.")
        await trace.settled()
        yield a_line(3, FIRST_HEARD_BACK)
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert EchoDropped(FIRST_HEARD_BACK) in place.trace.events
    assert place.trace.asked == [(QUESTION, False), ("Danke.", True)]


@pytest.mark.parametrize(
    "echo_seq", [pytest.param(2, id="the same seq"), pytest.param(1, id="a seq below")]
)
def test_an_echo_that_breaks_its_order_ends_the_loop_closed_and_silent(place, echo_seq):
    trace = place.trace
    gateway = PacedGateway(trace, [piece(f"{FIRST} {SECOND}"), hang])
    voice = RecordingVoice(trace, held={FIRST})

    async def hearing(trace):
        yield a_line(2, QUESTION)
        await while_its_first_sentence_plays(trace, voice)
        yield a_line(echo_seq, FIRST_HEARD_BACK)

    ending = place.run(hearing, gateway, voice=voice)

    assert ending.raised.subgroup(ValueError) is not None
    assert ("stopped", FIRST) in trace.calls
    assert ("closed",) in trace.entries


@pytest.mark.parametrize(
    ("after_the_answer", "missed_before"),
    [
        pytest.param([a_line(2, FIRST_HEARD_BACK), a_line(3, "Danke.")], None, id="an echo"),
        pytest.param([a_line(3, "Danke.")], 1, id="a gap"),
        pytest.param(
            [a_line(2, FIRST_HEARD_BACK), a_line(4, "Danke.")], 1, id="an echo, then a gap"
        ),
        pytest.param(
            [a_line(3, FIRST_HEARD_BACK), a_line(4, "Danke.")], 1, id="a gap, then an echo"
        ),
    ],
)
def test_the_line_after_an_echo_counts_only_the_lines_never_heard_as_missed(
    place, after_the_answer, missed_before
):
    gateway = FakeGateway(completion(FIRST), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, QUESTION)
        await trace.settled()
        for line in after_the_answer:
            yield line
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    thanks = next(row for row in rows_sent(gateway.requests[-1]) if row["text"] == "Danke.")
    assert thanks.get("missed_before") == missed_before
