"""An act line through the voice loop: told to the model, never asked, never a wake."""

import pytest

from agents.backend.fake_gateway import FakeGateway, completion
from tests.support import voice_support
from tests.support.voice_lines import a_line
from tests.support.voice_support import RETURNED, conversation_of, messages_of, rows_of

place = voice_support.place


@pytest.mark.parametrize("own", [False, True], ids=["an act of another", "an own act"])
def test_an_act_line_the_rule_would_wake_is_never_asked_and_reaches_the_model_marked(place, own):
    gateway = FakeGateway(completion("Gern."))

    async def hearing(trace):
        yield a_line(
            1, "listener legt ab.", name="Ben", kind="move", spoken=False, act=True, own=own
        )
        await trace.settled()
        assert gateway.requests == []
        yield a_line(2, "Danke.")
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert place.trace.asked == [("Danke.", False)]
    [message] = messages_of(gateway.requests[0])[1:]
    assert [(row["text"], row.get("act")) for row in rows_of(message)] == [
        ("listener legt ab.", True),
        ("Danke.", None),
    ]


def test_an_act_line_after_an_answer_leaves_the_following_short_reply_the_first_line_after(place):
    gateway = FakeGateway(completion("Eins."), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Frage?")
        await trace.settled()
        yield a_line(2, "Ben legt ab.", name="Ben", kind="move", spoken=False, act=True)
        yield a_line(3, "Ja.", name="Ben")
        await trace.settled()

    def wakes(line, *, first_after_answer):
        return first_after_answer or line.text == "Frage?"

    assert place.run(hearing, gateway, wakes=wakes) == RETURNED

    assert place.trace.asked == [("Frage?", False), ("Ja.", True)]
    assert conversation_of(gateway.requests[1])[-1] == ("user", ["Ben legt ab.", "Ja."])
