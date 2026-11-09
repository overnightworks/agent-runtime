"""The standard wake rule: a line wakes the agent when it names it, asks, or follows its answer."""

import asyncio

import pytest

from agent_runtime_client.backend.fake_gateway import FakeGateway, completion
from agent_runtime_client.backend.gateway import GatewayBackend
from agent_runtime_client.voice.loop import run_voice_loop
from agent_runtime_client.voice.wake import addressed_or_asked
from tests.support import voice_support
from tests.support.gateway_turn_support import a_gateway_config, a_person
from tests.support.virtual_time import VirtualTimeLoop
from tests.support.voice_support import CONTEXT, RecordingVoice, a_line, messages_of, rows_of

place = voice_support.place

SPEAKERS = [
    pytest.param({"name": "Ben", "kind": "person"}, id="a person's line"),
    pytest.param({"name": "Mia", "kind": "agent"}, id="another agent's line"),
]


@pytest.mark.parametrize(
    ("text", "names", "expected"),
    [
        pytest.param("Otto, are you there", {"Otto"}, True, id="named"),
        pytest.param("hey OTTO", {"Otto"}, True, id="other case"),
        pytest.param("what do you think, otto?!", {"Otto"}, True, id="lower case"),
        pytest.param("@Otto", {"Otto"}, True, id="mention"),
        pytest.param("Otto's view", {"Otto"}, True, id="possessive"),
        pytest.param("Ottoman rugs", {"Otto"}, False, id="longer word"),
        pytest.param("Motto", {"Otto"}, False, id="inside a word"),
        pytest.param("Good morning", {"Otto"}, False, id="not named"),
        pytest.param("Otto", {"Ot.o"}, False, id="the name is no pattern"),
        pytest.param("an Ot.o here", {"Ot.o"}, True, id="a name with a dot"),
        pytest.param("STRASSE", {"Straße"}, True, id="case folding"),
        pytest.param("Herr Strauß", {"Strauss"}, True, id="case folding of the line"),
        pytest.param("ask Ada now", {"Otto", "Ada"}, True, id="the other of two names"),
        pytest.param("ask ada lovelace now", {"Ada Lovelace"}, True, id="a name with a space"),
        pytest.param("ask Ada now", {"Ada Lovelace"}, False, id="half a name with a space"),
        pytest.param("ANAÏS, look", {"Anaïs"}, True, id="non-ASCII letters in another case"),
        pytest.param("Anaïs is here", {"Ana"}, False, id="a non-ASCII letter after the name"),
        pytest.param("Élise is here", {"Lise"}, False, id="a non-ASCII letter before the name"),
    ],
)
@pytest.mark.parametrize("speaker", SPEAKERS)
def test_a_line_naming_one_of_the_names_as_a_whole_word_in_any_case_wakes(
    text, names, expected, speaker
):
    wakes = addressed_or_asked(frozenset(names))

    assert wakes(a_line(1, text, **speaker), first_after_answer=False) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Is anyone there?", True, id="ends in a question mark"),
        pytest.param("Is anyone there?   ", True, id="trailing spaces"),
        pytest.param('She asked: "is anyone there?"', True, id="a closing quote"),
        pytest.param("She asked: “is it?” ", True, id="a curly closing quote and a space"),
        pytest.param("Er fragte: „Ist da wer?“", True, id="a German closing quote"),
        pytest.param("Why? Because it rained.", False, id="a question mark mid-line"),
    ],
)
@pytest.mark.parametrize("speaker", SPEAKERS)
def test_a_line_ending_in_a_question_mark_wakes_and_one_asking_only_mid_line_does_not(
    text, expected, speaker
):
    wakes = addressed_or_asked(frozenset({"Otto"}))

    assert wakes(a_line(1, text, **speaker), first_after_answer=False) is expected


@pytest.mark.parametrize(
    "names", [frozenset(), frozenset({"Otto", ""})], ids=["no name", "an empty name"]
)
def test_a_rule_without_a_name_or_with_an_empty_one_is_refused(names):
    with pytest.raises(ValueError):
        addressed_or_asked(names)


def test_in_the_loop_a_named_asked_or_first_line_after_a_played_answer_wakes_and_all_are_heard(
    place,
):
    voice = RecordingVoice(place.trace, held={"Gern."})
    gateway = FakeGateway(
        completion("Ja."), completion("Gern."), completion("[silent]"), completion("Gleich drei.")
    )

    heard = [
        a_line(1, "Guten Morgen."),
        a_line(2, "Otto, hörst du mich.", name="Ben"),
        a_line(3, "Gut."),
        a_line(4, "Und sonst.", name="Ben"),
        a_line(5, "Schön."),
        a_line(6, "Schönes Wetter.", name="Ben"),
        a_line(7, "Wie spät ist es?", name="Mia", kind="agent"),
    ]

    async def hearing(trace):
        for line in heard:
            yield line
            if line.text == "Und sonst.":  # heard while the answer to "Gut." plays
                voice.release("Gern.")
            await trace.settled()

    backend = GatewayBackend(a_gateway_config(place.directory), transport=gateway)
    wakes = addressed_or_asked(frozenset({"Otto"}))
    loop = run_voice_loop(
        place.agent(),
        backend,
        a_person(),
        hearing(place.trace),
        voice,
        wakes=wakes,
        context=CONTEXT,
        report=place.trace.record,
    )
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        runner.run(loop)

    assert place.trace.played == ["Ja.", "Gern.", "Gleich drei."]
    assert [lines_since_the_last_decision(request) for request in gateway.requests] == [
        ["Guten Morgen.", "Otto, hörst du mich."],
        ["Gut."],
        ["Und sonst.", "Schön."],
        ["Schönes Wetter.", "Wie spät ist es?"],
    ]


def lines_since_the_last_decision(request) -> list[str]:
    return [row["text"] for row in rows_of(messages_of(request)[-1])]
