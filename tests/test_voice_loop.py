"""The voice loop through its entry point: lines in, one decision at a time, sentences out."""

import asyncio
import random

import httpx2
import pytest

from agent_runtime_client.backend import TurnStopKind
from agent_runtime_client.backend.fake_gateway import (
    FakeGateway,
    Reply,
    completion,
    tool_call,
    tool_round,
)
from agent_runtime_client.backend.gateway import GatewayDetail
from agent_runtime_client.tools import HandTool, ToolDeclaration, ToolEffect, ToolOutcome
from agent_runtime_client.turn import FailureCode
from agent_runtime_client.voice import (
    DecisionFailed,
    ReplyUnreadable,
    SentenceRefused,
    SentenceStarted,
    StayedSilent,
    VoiceRefused,
    Woke,
)
from agent_runtime_client.voice.reply import PROTOCOL_TEXT
from tests.support import voice_support
from tests.support.gateway_turn_support import FailingTransport
from tests.support.voice_support import (
    CONTEXT,
    PROMPT,
    RETURNED,
    STREAMED_ANSWERS,
    PacedGateway,
    RecordingVoice,
    a_line,
    finish,
    hang,
    messages_of,
    piece,
    random_pieces,
    rows_of,
    written_piece_by_piece,
)

place = voice_support.place


def texts_of(message: dict) -> list[str]:
    return [row["text"] for row in rows_of(message)]


def conversation_of(request) -> list[tuple[str, list[str] | str]]:
    """A request's messages after the system one: a user message as its lines' texts."""
    return [
        (message["role"], texts_of(message) if message["role"] == "user" else message["content"])
        for message in messages_of(request)[1:]
    ]


def a_lookup_tool(trace) -> HandTool:
    async def look_up(principal, arguments):
        trace.record(("tool", "look_up"))
        return ToolOutcome(content="gefunden", is_error=False)

    declaration = ToolDeclaration(
        name="look_up",
        description="Looks something up.",
        parameters={"type": "object", "properties": {}},
        effect=ToolEffect.READ_ONLY,
    )
    return HandTool(declaration=declaration, handler=look_up)


def test_a_spoken_line_that_wakes_sends_one_request_with_prompt_protocol_context_and_the_line(
    place,
):
    gateway = FakeGateway(completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Kannst du helfen?")
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    [request] = gateway.requests
    system, *messages = messages_of(request)
    assert system["content"] == f"{PROMPT}\n\n{PROTOCOL_TEXT}\n\n{CONTEXT}"
    assert [message["role"] for message in messages] == ["user"]
    assert rows_of(messages[0]) == [
        {"name": "Ada", "kind": "person", "own": False, "spoken": True, "text": "Kannst du helfen?"}
    ]
    assert place.trace.events == [Woke(), SentenceStarted("Gern.")]


def test_a_line_that_does_not_wake_sends_nothing_and_leads_the_next_decisions_message(place):
    gateway = FakeGateway(completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Guten Morgen.")
        yield a_line(2, "Wie war das Wochenende?", name="Ben")
        await trace.settled()
        assert gateway.requests == []
        yield a_line(3, "Kannst du helfen?")
        await trace.settled()

    assert place.run(hearing, gateway, waking={"Kannst du helfen?"}) == RETURNED

    [request] = gateway.requests
    assert conversation_of(request) == [
        ("user", ["Guten Morgen.", "Wie war das Wochenende?", "Kannst du helfen?"])
    ]


def test_sentences_play_in_order_and_the_first_starts_while_the_reply_is_still_written(place):
    trace = place.trace
    sentence_one_started = SentenceStarted("Eins.")
    gateway = PacedGateway(
        trace,
        [
            piece("Eins. Zw"),
            lambda: trace.until(lambda: sentence_one_started in trace.entries),
            piece("ei. Dr"),
            piece("ei."),
            finish(),
        ],
    )

    async def hearing(trace):
        yield a_line(1, "Zähl bis drei.")
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert trace.calls == [
        (call, sentence)
        for sentence in ("Eins.", "Zwei.", "Drei.")
        for call in ("render", "play", "played")
    ]
    assert trace.entries.index(sentence_one_started) < trace.entries.index(("closed",))


def test_a_silent_reply_speaks_nothing_runs_its_tools_and_is_recorded_as_silent(place):
    gateway = FakeGateway(
        tool_round(tool_call("call-1", "look_up", {}), content="[silent]"),
        completion("[silent]"),
        completion("Gern."),
    )

    async def hearing(trace):
        yield a_line(1, "Ada erzählt vom Urlaub.")
        await trace.settled()
        yield a_line(2, "Was meinst du?")
        await trace.settled()

    tools = [a_lookup_tool(place.trace)]
    assert place.run(hearing, gateway, tools=tools) == RETURNED

    assert place.trace.count(("tool", "look_up")) == 1
    assert place.trace.calls[0] == ("render", "Gern.")
    assert place.trace.events[:3] == [Woke(), StayedSilent(), StayedSilent()]
    assert conversation_of(gateway.requests[2]) == [
        ("user", ["Ada erzählt vom Urlaub."]),
        ("assistant", "[silent]"),
        ("user", ["Was meinst du?"]),
    ]


def test_a_tool_rounds_text_plays_and_its_rest_is_released_when_the_round_ends(place):
    gateway = FakeGateway(
        tool_round(tool_call("call-1", "look_up", {}), content="Einen Moment. Ich schaue"),
        completion("[silent]"),
        completion("Gern."),
    )

    async def hearing(trace):
        yield a_line(1, "Wann fährt der Zug?")
        await trace.settled()
        yield a_line(2, "Und?")
        await trace.settled()

    tools = [a_lookup_tool(place.trace)]
    assert place.run(hearing, gateway, tools=tools) == RETURNED

    assert place.trace.played[:2] == ["Einen Moment.", "Ich schaue"]
    assert place.trace.count(("tool", "look_up")) == 1
    assert StayedSilent() in place.trace.events
    assert conversation_of(gateway.requests[2])[1] == ("assistant", "Einen Moment. Ich schaue")


def test_an_own_spoken_line_never_enters_and_an_own_written_line_enters_but_never_wakes(place):
    gateway = FakeGateway(completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Das habe ich gesagt.", name="listener", kind="agent", own=True)
        yield a_line(
            2, "Das habe ich geschrieben.", name="listener", kind="agent", own=True, spoken=False
        )
        await trace.settled()
        assert gateway.requests == []
        yield a_line(3, "Danke.")
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    [request] = gateway.requests
    [message] = messages_of(request)[1:]
    assert [(row["text"], row["own"], row["spoken"]) for row in rows_of(message)] == [
        ("Das habe ich geschrieben.", True, False),
        ("Danke.", False, True),
    ]
    assert place.trace.asked == [("Danke.", False)]


def test_a_gap_in_seq_marks_the_next_line_with_how_many_were_missed(place):
    gateway = FakeGateway(completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Eins.")
        yield a_line(2, "Gesagt.", name="listener", kind="agent", own=True)
        yield a_line(3, "Drei.")
        yield a_line(6, "Sechs.")
        yield a_line(9, "Selbst gesagt.", name="listener", kind="agent", own=True)
        yield a_line(11, "Elf?")
        await trace.settled()

    assert place.run(hearing, gateway, waking={"Elf?"}) == RETURNED

    [message] = messages_of(gateway.requests[0])[1:]
    assert [row.get("missed_before") for row in rows_of(message)] == [None, None, 2, 3]


def test_two_lines_heard_while_deciding_of_which_one_wakes_get_exactly_one_more_decision(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [lambda: trace.until(lambda: len(trace.asked) == 3), piece("Gern."), finish()],
        [piece("Auch das."), finish()],
    )

    async def hearing(trace):
        yield a_line(1, "Frage eins?")
        await trace.until(lambda: Woke() in trace.events)
        yield a_line(2, "Ach so.", name="Ben")
        yield a_line(3, "Frage zwei?", name="Ben")
        await trace.settled()

    assert place.run(hearing, gateway, waking={"Frage eins?", "Frage zwei?"}) == RETURNED

    assert len(gateway.requests) == 2
    assert conversation_of(gateway.requests[1]) == [
        ("user", ["Frage eins?"]),
        ("assistant", "Gern."),
        ("user", ["Ach so.", "Frage zwei?"]),
    ]


def test_lines_heard_while_deciding_that_do_not_wake_start_nothing_and_lead_the_next(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [lambda: trace.until(lambda: len(trace.asked) == 3), piece("Gern."), finish()],
        [piece("Auch das."), finish()],
    )

    async def hearing(trace):
        yield a_line(1, "Frage eins?")
        await trace.until(lambda: Woke() in trace.events)
        yield a_line(2, "Ach so.", name="Ben")
        yield a_line(3, "Na gut.", name="Ben")
        await trace.settled()
        assert len(gateway.requests) == 1
        yield a_line(4, "Frage zwei?")
        await trace.settled()

    assert place.run(hearing, gateway, waking={"Frage eins?", "Frage zwei?"}) == RETURNED

    assert len(gateway.requests) == 2
    assert conversation_of(gateway.requests[1])[-1] == (
        "user",
        ["Ach so.", "Na gut.", "Frage zwei?"],
    )


def test_a_waking_line_heard_while_an_answer_plays_is_decided_after_it_plays_uncut(place):
    voice = RecordingVoice(place.trace, held={"Eins."})
    gateway = FakeGateway(completion("Eins. Zwei."), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield a_line(2, "Und dann?", name="Ben")
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.played == ["Eins.", "Zwei.", "Gern."]
    assert place.trace.events.index(Woke(), 1) > place.trace.events.index(SentenceStarted("Zwei."))
    assert conversation_of(gateway.requests[1]) == [
        ("user", ["Erzähl."]),
        ("assistant", "Eins. Zwei."),
        ("user", ["Und dann?"]),
    ]


def test_two_hundred_waking_lines_each_after_the_last_answer_get_two_hundred_decisions(place):
    gateway = FakeGateway(completion("Gut."))

    async def hearing(trace):
        for seq in range(1, 201):
            yield a_line(seq, f"Zeile {seq}?")
            await trace.until(lambda: len(trace.played) == seq)
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert place.trace.events.count(Woke()) == 200
    assert len(gateway.requests) == 200
    assert conversation_of(gateway.requests[-1])[-2:] == [
        ("assistant", "Gut."),
        ("user", ["Zeile 200?"]),
    ]


def test_the_first_line_after_an_answer_that_played_is_the_one_marked_first_after_answer(place):
    voice = RecordingVoice(place.trace, held={"Eins.", "Gern."})
    gateway = FakeGateway(completion("Eins. Zwei."), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Frage?")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield a_line(2, "Zwischenfrage?", name="Ben")
        voice.release("Eins.")
        await trace.until(lambda: ("play", "Gern.") in trace.entries)
        yield a_line(3, "Noch was.", name="Ben")
        voice.release("Gern.")
        await trace.settled()
        yield a_line(4, "Notiert.", name="listener", kind="agent", own=True, spoken=False)
        yield a_line(5, "Danke.")
        yield a_line(6, "Weiter im Text.", name="Ben")
        await trace.settled()

    waking = {"Frage?", "Zwischenfrage?"}
    assert place.run(hearing, gateway, voice=voice, waking=waking) == RETURNED

    assert place.trace.asked == [
        ("Frage?", False),
        ("Zwischenfrage?", False),
        ("Noch was.", False),
        ("Danke.", True),
        ("Weiter im Text.", False),
    ]


A_BUDGET_REFUSAL = Reply(status=400, body=b'{"error": {"type": "budget_exceeded"}}')


@pytest.mark.parametrize(
    ("first_reply", "voice_raises", "signature"),
    [
        (completion("[silent]"), {}, StayedSilent()),
        (completion("[shout] Hallo."), {}, ReplyUnreadable()),
        (completion("Eins. Zwei."), {("play", "Eins."): VoiceRefused()}, SentenceRefused("Eins.")),
        (
            A_BUDGET_REFUSAL,
            {},
            DecisionFailed(
                TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, GatewayDetail.GATEWAY_REFUSED
            ),
        ),
    ],
)
def test_no_line_after_a_decision_that_played_nothing_is_marked_first_after_answer(
    place, first_reply, voice_raises, signature
):
    voice = RecordingVoice(place.trace, raises=voice_raises)
    gateway = FakeGateway(first_reply, completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Frage?")
        await trace.settled()
        yield a_line(2, "Danke.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice, waking={"Frage?"}) == RETURNED

    assert signature in place.trace.events
    assert place.trace.asked == [("Frage?", False), ("Danke.", False)]


A_REFUSED_TURN = DecisionFailed(
    TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, GatewayDetail.GATEWAY_REFUSED
)
A_SILENT_ROUND = tool_round(tool_call("call-1", "look_up", {}), content="[silent]")


@pytest.mark.parametrize(
    ("replies", "voice_raises", "events"),
    [
        ((completion("[shout] Hallo."),), {}, [Woke(), ReplyUnreadable()]),
        (
            (completion("Eins. Zwei."),),
            {("play", "Eins."): VoiceRefused()},
            [Woke(), SentenceStarted("Eins."), SentenceRefused("Eins.")],
        ),
        ((A_BUDGET_REFUSAL,), {}, [Woke(), A_REFUSED_TURN]),
        (
            (A_SILENT_ROUND, A_BUDGET_REFUSAL),
            {},
            [Woke(), StayedSilent(), A_REFUSED_TURN],
        ),
        (
            (A_SILENT_ROUND, completion("Eins.")),
            {("play", "Eins."): VoiceRefused()},
            [Woke(), StayedSilent(), SentenceStarted("Eins."), SentenceRefused("Eins.")],
        ),
        (
            (A_SILENT_ROUND, completion("[shout] Hallo.")),
            {},
            [Woke(), StayedSilent(), ReplyUnreadable()],
        ),
    ],
)
def test_a_decision_that_played_nothing_leaves_its_lines_undecided_until_a_line_wakes(
    place, replies, voice_raises, events
):
    voice = RecordingVoice(place.trace, raises=voice_raises)
    gateway = FakeGateway(*replies, completion("Gern."))
    tools = [a_lookup_tool(place.trace)]
    quiet = [f"Leise {number}." for number in range(1, 6)]

    async def hearing(trace):
        yield a_line(1, "Frage?")
        await trace.settled()
        for seq, text in enumerate(quiet, start=2):
            yield a_line(seq, text, name="Ben")
        await trace.settled()
        assert len(gateway.requests) == len(replies)
        yield a_line(7, "Noch eine Frage?")
        await trace.settled()

    waking = {"Frage?", "Noch eine Frage?"}
    assert place.run(hearing, gateway, voice=voice, waking=waking, tools=tools) == RETURNED

    assert place.trace.events[: len(events)] == events
    assert len(gateway.requests) == len(replies) + 1
    assert conversation_of(gateway.requests[-1]) == [
        ("user", ["Frage?", *quiet, "Noch eine Frage?"])
    ]


def test_a_line_heard_during_a_decision_that_played_nothing_follows_that_decisions_lines(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [lambda: trace.until(lambda: len(trace.asked) == 2), piece("[shout] Hallo."), finish()],
        [piece("Gern."), finish()],
    )

    async def hearing(trace):
        yield a_line(1, "Frage?")
        await trace.until(lambda: Woke() in trace.events)
        yield a_line(2, "Dazwischen.", name="Ben")
        await trace.settled()
        yield a_line(3, "Noch eine Frage?")
        await trace.settled()

    assert place.run(hearing, gateway, waking={"Frage?", "Noch eine Frage?"}) == RETURNED

    assert conversation_of(gateway.requests[1]) == [
        ("user", ["Frage?", "Dazwischen.", "Noch eine Frage?"])
    ]


def test_a_refused_render_ends_the_answer_closes_the_turn_and_keeps_what_played(place):
    trace = place.trace
    voice = RecordingVoice(trace, raises={("render", "Zwei."): VoiceRefused()})
    gateway = PacedGateway(
        trace, [piece("Eins. Zwei. Drei. Vier"), hang], [piece("Gern."), finish()]
    )

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()
        yield a_line(2, "Und?")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.events[:3] == [Woke(), SentenceStarted("Eins."), SentenceRefused("Zwei.")]
    assert ("render", "Drei.") not in trace.calls
    second_decision = trace.entries.index(Woke(), trace.entries.index(Woke()) + 1)
    assert trace.entries.index(("closed",)) < second_decision
    assert conversation_of(gateway.requests[1])[1] == ("assistant", "Eins.")


def test_a_turn_stopped_after_a_sentence_played_plays_out_the_queued_one_and_drops_the_rest(
    place,
):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [
            piece("Eins. Zw"),
            lambda: trace.until(lambda: "Eins." in trace.played),
            piece("ei. Dr"),
            httpx2.ReadError("cut"),
        ],
        [piece("Gern."), finish()],
    )
    voice = RecordingVoice(trace, held={"Zwei."})

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: any(isinstance(event, DecisionFailed) for event in trace.events))
        voice.release("Zwei.")
        await trace.settled()
        yield a_line(2, "Und?")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert [sentence for call, sentence in trace.calls if call == "render"] == [
        "Eins.",
        "Zwei.",
        "Gern.",
    ]
    assert trace.played == ["Eins.", "Zwei.", "Gern."]
    assert (
        DecisionFailed(
            TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, GatewayDetail.CONNECTION_FAILED
        )
        in trace.events
    )
    assert conversation_of(gateway.requests[1])[1] == ("assistant", "Eins. Zwei.")


def test_playing_longer_than_the_turns_limit_plays_every_sentence_without_a_limit_failure(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [
            lambda: asyncio.sleep(1),
            piece("Eins. Zw"),
            lambda: asyncio.sleep(1),
            piece("ei. Dr"),
            piece("ei."),
            finish(),
        ],
    )
    voice = RecordingVoice(trace, seconds_per_sentence=30)
    clock = []

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: len(trace.played) == 3)
        clock.append(asyncio.get_running_loop().time())

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.agent().max_duration_seconds < clock[0]
    assert trace.played == ["Eins.", "Zwei.", "Drei."]
    assert not any(isinstance(event, DecisionFailed) for event in trace.events)


def test_a_backend_error_other_than_a_stopped_turn_is_raised(place):
    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()

    ending = place.run(hearing, FailingTransport(RuntimeError("broken")))

    assert ending.raised.subgroup(RuntimeError) is not None
    assert place.trace.events == [Woke()]


def speaking_while_writing(place) -> tuple[PacedGateway, RecordingVoice]:
    """A turn still open after its first sentence, which plays until released."""
    trace = place.trace
    return PacedGateway(trace, [piece("Eins. Zwei"), hang]), RecordingVoice(trace, held={"Eins."})


class HearingBroke(Exception):
    pass


@pytest.mark.parametrize(
    ("next_line", "error"),
    [
        (a_line(5, "Noch einmal."), ValueError),
        (a_line(4, "Früher."), ValueError),
        (HearingBroke(), HearingBroke),
    ],
)
def test_a_hearing_that_breaks_its_order_or_raises_ends_the_loop_closed_and_silent(
    place, next_line, error
):
    gateway, voice = speaking_while_writing(place)

    async def hearing(trace):
        yield a_line(5, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        if isinstance(next_line, Exception):
            raise next_line
        yield next_line

    ending = place.run(hearing, gateway, voice=voice)

    assert ending.raised.subgroup(error) is not None
    assert ("stopped", "Eins.") in place.trace.calls
    assert ("closed",) in place.trace.entries


@pytest.mark.parametrize("call", ["render", "play"])
def test_a_voice_that_raises_another_error_closes_the_turn_and_raises_it(place, call):
    trace = place.trace
    gateway = PacedGateway(trace, [piece("Eins. Zwei"), hang])
    voice = RecordingVoice(trace, raises={(call, "Eins."): RuntimeError("no sound")})

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await hang()

    ending = place.run(hearing, gateway, voice=voice)

    assert ending.raised.subgroup(RuntimeError) is not None
    assert ("closed",) in trace.entries
    assert not any(isinstance(event, SentenceRefused) for event in trace.events)


def arranged(place, state: str) -> tuple[PacedGateway, RecordingVoice]:
    """A turn and a voice that leave the loop idle, deciding or playing once the line woke it."""
    trace = place.trace
    if state == "playing":
        return speaking_while_writing(place)
    reply = [piece("Gern."), finish()] if state == "idle" else [hang]
    return PacedGateway(trace, reply), RecordingVoice(trace)


@pytest.mark.parametrize(
    ("state", "stopped"), [("idle", []), ("deciding", []), ("playing", [("stopped", "Eins.")])]
)
def test_when_hearing_ends_the_loop_closes_the_turn_stops_the_sound_and_returns(
    place, state, stopped
):
    gateway, voice = arranged(place, state)

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert ("closed",) in place.trace.entries
    assert [call for call in place.trace.calls if call[0] == "stopped"] == stopped


@pytest.mark.parametrize(
    ("state", "stopped"), [("deciding", []), ("playing", [("stopped", "Eins.")])]
)
def test_the_hosts_cancel_closes_the_turn_stops_the_sound_and_reports_nothing_more(
    place, state, stopped
):
    gateway, voice = arranged(place, state)

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()
        await trace.host_cancels()

    assert place.run(hearing, gateway, voice=voice).cancelled

    entries = place.trace.entries
    assert entries.index(("closed",)) > entries.index(("cancelled",))
    assert [call for call in place.trace.calls if call[0] == "stopped"] == stopped
    assert place.trace.events_after(("cancelled",)) == []


@pytest.mark.parametrize("call", ["render", "play"])
def test_a_voice_that_never_returns_still_hears_lines_but_starts_no_decision_until_cancelled(
    place, call
):
    gateway = FakeGateway(completion("Eins. Zwei."), completion("Gern."))
    voice = RecordingVoice(place.trace, hangs={(call, "Eins.")})

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()
        yield a_line(2, "Hallo?", name="Ben")
        yield a_line(3, "Hörst du mich?", name="Ben")
        await trace.settled()
        await trace.host_cancels()

    assert place.run(hearing, gateway, voice=voice).cancelled

    assert [text for text, _ in place.trace.asked] == ["Erzähl.", "Hallo?", "Hörst du mich?"]
    assert len(gateway.requests) == 1
    assert place.trace.events_after(("cancelled",)) == []


CUT_ANYWHERE = [
    *((text, sentences, None) for text, sentences in STREAMED_ANSWERS),
    ("  [ Silent ] Nichts zu sagen.", [], StayedSilent()),
    ("[shout] Hallo.", [], ReplyUnreadable()),
    ("[" + "x" * 17 + "] Hallo.", [], ReplyUnreadable()),
    ("[sile", [], ReplyUnreadable()),
    ("Ja [silent] gut.", ["Ja [silent] gut."], None),
]


@pytest.mark.parametrize(("text", "sentences", "opening"), CUT_ANYWHERE)
def test_a_reply_cut_anywhere_plays_each_sentence_once_whole_and_after_content(
    place, text, sentences, opening
):
    trace = place.trace
    chooser = random.Random(text)  # noqa: S311 - seeded cut points that repeat on every run
    cuts = [random_pieces(text, chooser) for _ in range(8)]
    gateway = PacedGateway(trace, *(written_piece_by_piece(trace, texts) for texts in cuts))

    async def hearing(trace):
        for seq in range(1, len(cuts) + 1):
            yield a_line(seq, "Erzähl.")
            await trace.until(lambda: trace.count(("closed",)) == seq)
            await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert trace.played == sentences * len(cuts)
    assert trace.rendered_at_the_written_end == sentences[-1:] * len(cuts)
    markers = [event for event in trace.events if isinstance(event, StayedSilent | ReplyUnreadable)]
    assert markers == ([opening] * len(cuts) if opening else [])


def test_a_reply_streamed_slowly_over_a_real_socket_plays_the_same_sentences(place):
    trace = place.trace
    parts = [
        piece("Eins. Zw"),
        lambda: trace.until(lambda: SentenceStarted("Eins.") in trace.entries),
        piece("ei. Dr"),
        lambda: trace.until(lambda: SentenceStarted("Zwei.") in trace.entries),
        piece("ei."),
        finish(),
    ]

    async def hearing(trace):
        yield a_line(1, "Zähl bis drei.")
        await trace.until(lambda: len(trace.played) == 3)

    assert place.run_over_a_socket(hearing, parts) == RETURNED

    assert trace.played == ["Eins.", "Zwei.", "Drei."]
    assert not any(isinstance(event, DecisionFailed) for event in trace.events)
