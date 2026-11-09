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
from tests.support import voice_support
from tests.support.voice_lines import a_line
from tests.support.voice_support import (
    A_GATEWAY_REFUSAL,
    RETURNED,
    PacedGateway,
    RecordingVoice,
    finish,
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
    pytest.param("Ja, genau", ["Ja, gerne"], True, id="a short word with its comma is long"),
    pytest.param("geht's", ["gehts"], False, id="punctuation inside a word stays"),
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

PUNCTUATION_AND_CASE = [
    pytest.param("Danke schön", ["Danke, schön."], True, id="a comma and a period"),
    pytest.param("Umsatz? Kosten!", ["Der Umsatz, die Kosten."], True, id="other punctuation"),
    pytest.param("„Gern geschehen…“", ["Gern geschehen."], True, id="quotes and an ellipsis"),
    pytest.param("DANKE SCHÖN!", ["Danke, schön."], True, id="case and punctuation together"),
]


@pytest.mark.parametrize(("text", "answer", "expected"), COUNTED_WORDS)
def test_a_spoken_line_is_an_echo_when_half_its_long_words_are_words_of_the_answer(
    text, answer, expected
):
    assert is_echo(a_line(1, text), answer) is expected


@pytest.mark.parametrize(("text", "answer", "expected"), PUNCTUATION_AND_CASE)
def test_case_and_the_punctuation_at_word_edges_do_not_hide_an_echo(text, answer, expected):
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


def lines_sent(request) -> list[str]:
    """The text of every line a request sent, in order."""
    return [
        row["text"]
        for message in messages_of(request)[1:]
        if message["role"] == "user"
        for row in rows_of(message)
    ]


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
