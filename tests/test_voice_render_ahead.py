"""The voice loop renders the next sentence while the one before it plays, one ahead at most."""

import asyncio
import functools
import itertools

import httpx2
import pytest

from agent_runtime_client.backend import TurnStopKind
from agent_runtime_client.backend.fake_gateway import FakeGateway, completion
from agent_runtime_client.backend.gateway import GatewayDetail
from agent_runtime_client.turn import FailureCode
from agent_runtime_client.voice import (
    DecisionFailed,
    EchoDropped,
    SentenceRefused,
    SentenceStarted,
    VoiceRefused,
    Woke,
)
from tests.support import voice_support
from tests.support.voice_lines import STREAMED_ANSWERS, a_line
from tests.support.voice_support import (
    RETURNED,
    PacedGateway,
    RecordingVoice,
    StreamPart,
    Trace,
    conversation_of,
    finish,
    piece,
)

place = voice_support.place


def written_while_the_first_sentence_plays(trace: Trace, texts: list[str]) -> list[StreamPart]:
    """A reply whose first piece is written at once and the rest once ``Eins.`` plays."""
    first, *rest = texts
    parts = [functools.partial(trace.written, first), piece(first)]
    parts.append(lambda: trace.until(lambda: ("play", "Eins.") in trace.entries))
    for text in rest:
        parts += [functools.partial(trace.written, text), piece(text)]
    return [*parts, finish()]


@pytest.mark.parametrize(
    "texts",
    [
        pytest.param(["Eins. Zwei. Drei."], id="written before the one before it plays"),
        pytest.param(["Eins. Zw", "ei. Drei."], id="written while the one before it plays"),
    ],
)
def test_the_next_sentence_renders_once_written_while_the_one_before_it_plays(place, texts):
    trace = place.trace
    gateway = PacedGateway(trace, written_while_the_first_sentence_plays(trace, texts))
    voice = RecordingVoice(trace, held={"Eins."})

    async def hearing(trace):
        yield a_line(1, "Zähl bis drei.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        await trace.settled()
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    entries = trace.entries
    assert (
        entries.index(("written", texts[-1]))
        < entries.index(("render", "Zwei."))
        < entries.index(("played", "Eins."))
    )


def test_a_sentence_two_ahead_renders_only_after_the_one_playing_has_played(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins.", "Zwei."})
    gateway = FakeGateway(completion("Eins. Zwei. Drei."))

    async def hearing(trace):
        yield a_line(1, "Zähl bis drei.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        await trace.settled()
        voice.release("Eins.")
        await trace.until(lambda: ("play", "Zwei.") in trace.entries)
        await trace.settled()
        voice.release("Zwei.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    calls = trace.calls
    assert (
        calls.index(("render", "Zwei."))
        < calls.index(("played", "Eins."))
        < calls.index(("render", "Drei."))
        < calls.index(("played", "Zwei."))
    )


@pytest.mark.parametrize(("text", "sentences"), STREAMED_ANSWERS)
def test_sentences_play_in_written_order_each_started_after_the_one_before_it_played(
    place, text, sentences
):
    trace = place.trace
    voice = RecordingVoice(trace, seconds_per_render=1, seconds_per_sentence=3)
    gateway = FakeGateway(completion(text))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: len(trace.played) == len(sentences))

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.played == sentences
    assert [event for event in trace.events if isinstance(event, SentenceStarted)] == [
        SentenceStarted(sentence) for sentence in sentences
    ]
    for before, after in itertools.pairwise(sentences):
        assert trace.entries.index(("played", before)) < trace.entries.index(SentenceStarted(after))


async def pause_between(trace: Trace, ended: str, next_one: str) -> float:
    """The loop's time from the end of ``ended`` to the start of ``next_one``."""
    await trace.until(lambda: ("played", ended) in trace.entries)
    ended_at = asyncio.get_running_loop().time()
    await trace.until(lambda: ("play", next_one) in trace.entries)
    return asyncio.get_running_loop().time() - ended_at


def test_a_sentence_after_the_first_plays_with_no_render_pause(place):
    trace = place.trace
    voice = RecordingVoice(trace, seconds_per_render=1, seconds_per_sentence=3)
    gateway = FakeGateway(completion("Eins. Zwei. Drei."))
    pauses = []

    async def hearing(trace):
        yield a_line(1, "Zähl bis drei.")
        pauses.append(await pause_between(trace, "Eins.", "Zwei."))
        pauses.append(await pause_between(trace, "Zwei.", "Drei."))
        await trace.until(lambda: len(trace.played) == 3)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert pauses == [0, 0]


def test_a_render_refused_while_the_one_before_it_plays_is_reported_after_that_one_played(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."}, raises={("render", "Zwei."): VoiceRefused()})
    gateway = FakeGateway(completion("Eins. Zwei. Drei."), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        await trace.settled()
        voice.release("Eins.")
        await trace.settled()
        yield a_line(2, "Und?")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    entries = trace.entries
    assert (
        entries.index(("render", "Zwei."))
        < entries.index(("played", "Eins."))
        < entries.index(SentenceRefused("Zwei."))
    )
    assert trace.events[:3] == [Woke(), SentenceStarted("Eins."), SentenceRefused("Zwei.")]
    assert ("render", "Drei.") not in trace.calls
    assert conversation_of(gateway.requests[1])[1] == ("assistant", "Eins.")


def test_a_refused_play_never_plays_starts_or_records_the_sentence_rendered_after_it(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Zwei."}, raises={("play", "Zwei."): VoiceRefused()})
    gateway = FakeGateway(completion("Eins. Zwei. Drei."), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Zwei.") in trace.entries)
        await trace.settled()
        voice.release("Zwei.")
        await trace.settled()
        yield a_line(2, "Und?")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert ("render", "Drei.") in trace.calls
    assert ("play", "Drei.") not in trace.calls
    assert trace.events[:4] == [
        Woke(),
        SentenceStarted("Eins."),
        SentenceStarted("Zwei."),
        SentenceRefused("Zwei."),
    ]
    assert SentenceStarted("Drei.") not in trace.events
    assert conversation_of(gateway.requests[1])[1] == ("assistant", "Eins.")


def test_a_turn_stopped_while_a_sentence_plays_still_plays_the_one_rendered_after_it(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [
            piece("Eins. Zwei. Dr"),
            lambda: trace.until(lambda: ("play", "Eins.") in trace.entries),
            trace.settled,
            httpx2.ReadError("cut"),
        ],
        [piece("Gern."), finish()],
    )
    voice = RecordingVoice(trace, held={"Eins."})
    stopped = DecisionFailed(
        TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, GatewayDetail.CONNECTION_FAILED
    )

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: stopped in trace.events)
        voice.release("Eins.")
        await trace.settled()
        yield a_line(2, "Und?")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    entries = trace.entries
    assert entries.index(("render", "Zwei.")) < entries.index(stopped)
    assert entries.index(stopped) < entries.index(("played", "Eins."))
    assert trace.played == ["Eins.", "Zwei.", "Gern."]
    assert conversation_of(gateway.requests[1])[1] == ("assistant", "Eins. Zwei.")


def test_the_hosts_cancel_stops_the_sentence_playing_and_the_render_after_it(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."}, hangs={("render", "Zwei.")})
    gateway = FakeGateway(completion("Eins. Zwei. Drei."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        await trace.settled()
        await trace.host_cancels()

    assert place.run(hearing, gateway, voice=voice).cancelled

    calls = trace.calls
    assert ("unrendered", "Zwei.") in calls
    assert ("stopped", "Eins.") in calls
    entries = trace.entries
    assert entries.index(("unrendered", "Zwei.")) < entries.index(("ended",))
    assert [call for call in calls if call[0] == "play"] == [("play", "Eins.")]
    assert trace.events_after(("cancelled",)) == []


def test_a_line_repeating_only_the_sentence_rendered_ahead_is_no_echo_and_enters(place):
    playing, rendered_ahead = "Die Folie zeigt den Umsatz.", "Danach kommen die Kosten."
    heard_back = "danach kommen die Kosten"
    voice = RecordingVoice(place.trace, held={playing})
    gateway = FakeGateway(completion(f"{playing} {rendered_ahead}"), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Was zeigt die Folie?")
        await trace.until(lambda: ("play", playing) in trace.entries)
        await trace.settled()
        yield a_line(2, heard_back, name="Ben")
        voice.release(playing)
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    entries = place.trace.entries
    assert (
        entries.index(("render", rendered_ahead))
        < entries.index(("asked", heard_back, False))
        < entries.index(("played", playing))
    )
    assert EchoDropped(heard_back) not in place.trace.events
    assert conversation_of(gateway.requests[1]) == [
        ("user", ["Was zeigt die Folie?"]),
        ("assistant", f"{playing} {rendered_ahead}"),
        ("user", [heard_back]),
    ]
