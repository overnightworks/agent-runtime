"""Through the voice loop's entry point, each decision sends only a window of the talk."""

import json
from itertools import pairwise

import pytest

from agent_runtime_client.backend.fake_gateway import FakeGateway, completion
from agent_runtime_client.voice.reply import WINDOW_NOTE
from tests.support.voice_lines import a_line
from tests.support.voice_support import A_BUDGET_REFUSAL, RETURNED, VoicePlace, messages_of

QUIET = [f"Leise {number:02d}." for number in range(1, 13)]


def bytes_of(messages: list[dict]) -> int:
    return sum(len(message["content"].encode()) for message in messages)


def sent_bytes(request) -> int:
    """What a request sent of the conversation, in UTF-8 bytes; the system text does not count."""
    return bytes_of(messages_of(request)[1:])


def extends(later, earlier) -> bool:
    """Whether ``later`` sent every message of ``earlier`` unchanged, the system one included."""
    sent = messages_of(earlier)
    return messages_of(later)[: len(sent)] == sent


def opens_with_the_note(request) -> bool:
    return messages_of(request)[1]["content"].split("\n")[0] == WINDOW_NOTE


def heard_texts(request) -> list[str]:
    """The text of every line a request sent, in order, without the window note."""
    rows = [
        row
        for message in messages_of(request)[1:]
        if message["role"] == "user"
        for row in message["content"].split("\n")
    ]
    return [json.loads(row)["text"] for row in rows if row != WINDOW_NOTE]


def a_talk(place: VoicePlace, decisions: int) -> list:
    """Waking lines of one size, each heard once the last answer played; the requests sent."""
    gateway = FakeGateway(completion("Gut."))

    async def hearing(trace):
        for seq in range(1, decisions + 1):
            yield a_line(seq, f"Zeile {seq:03d}?")
            await trace.until(lambda: len(trace.played) == seq)
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED
    return gateway.requests


def a_question_then_quiet_lines(place: VoicePlace) -> list:
    """A question answered, quiet lines that wake nothing, then a second question."""
    gateway = FakeGateway(completion("Gut."))

    async def hearing(trace):
        yield a_line(1, "Frage?")
        await trace.until(lambda: len(trace.played) == 1)
        for seq, text in enumerate(QUIET, start=2):
            yield a_line(seq, text, name="Ben")
        yield a_line(len(QUIET) + 2, "Noch eine Frage?")
        await trace.settled()

    assert place.run(hearing, gateway, waking={"Frage?", "Noch eine Frage?"}) == RETURNED
    return gateway.requests


def test_a_talk_within_the_window_drops_nothing_and_each_request_extends_the_last(tmp_path):
    unbounded = a_talk(VoicePlace(tmp_path), 12)
    window = sent_bytes(unbounded[-1])

    bounded = a_talk(VoicePlace(tmp_path, window_bytes=window), 12)

    assert [messages_of(request) for request in bounded] == [
        messages_of(request) for request in unbounded
    ]
    assert all(extends(later, earlier) for earlier, later in pairwise(bounded))


def test_a_request_beyond_the_window_drops_the_oldest_whole_pairs_until_a_quarter_is_free(
    tmp_path,
):
    unbounded = a_talk(VoicePlace(tmp_path), 12)
    window = sent_bytes(unbounded[-1]) - 1

    bounded = a_talk(VoicePlace(tmp_path, window_bytes=window), 12)

    assert [messages_of(request) for request in bounded[:-1]] == [
        messages_of(request) for request in unbounded[:-1]
    ]
    full_system, *full = messages_of(unbounded[-1])
    system, first, *rest = messages_of(bounded[-1])
    dropped = len(full) - len(rest) - 1
    assert system == full_system
    assert dropped > 0
    assert dropped % 2 == 0
    assert first == {**full[dropped], "content": f"{WINDOW_NOTE}\n{full[dropped]['content']}"}
    assert rest == full[dropped + 1 :]
    assert sent_bytes(bounded[-1]) * 4 <= window * 3
    youngest_dropped_pair = full[dropped - 2 : dropped]
    assert (sent_bytes(bounded[-1]) + bytes_of(youngest_dropped_pair)) * 4 > window * 3


def test_a_long_talk_drops_once_per_quarter_of_the_window_it_grows_never_once_per_line(tmp_path):
    window = 1000

    requests = a_talk(VoicePlace(tmp_path, window_bytes=window), 40)

    drops = [
        index
        for index, (earlier, later) in enumerate(pairwise(requests), start=1)
        if not extends(later, earlier)
    ]
    assert len(drops) >= 3
    assert all(later - earlier > 1 for earlier, later in pairwise(drops))
    assert max(sent_bytes(request) for request in requests) <= window
    for drop in drops:
        appended = messages_of(requests[drop])[-2:]
        assert sent_bytes(requests[drop - 1]) + bytes_of(appended) > window
        assert sent_bytes(requests[drop]) * 4 <= window * 3
        assert opens_with_the_note(requests[drop])


def test_lines_beyond_the_window_on_their_own_lose_their_oldest_and_keep_the_newest(tmp_path):
    unbounded = a_question_then_quiet_lines(VoicePlace(tmp_path))
    full_message = messages_of(unbounded[-1])[-1]["content"]
    full_lines = full_message.split("\n")
    window = len(full_message.encode()) // 2

    bounded = a_question_then_quiet_lines(VoicePlace(tmp_path, window_bytes=window))

    _system, only = messages_of(bounded[-1])
    note, *kept = only["content"].split("\n")
    assert note == WINDOW_NOTE
    assert 0 < len(kept) < len(full_lines)
    assert kept == full_lines[-len(kept) :]
    assert sent_bytes(bounded[-1]) <= window
    youngest_dropped = full_lines[-len(kept) - 1]
    assert sent_bytes(bounded[-1]) + len(f"\n{youngest_dropped}".encode()) > window


@pytest.mark.parametrize(("earlier", "noted"), [([], False), (["Frage?"], True)])
def test_a_line_larger_than_the_window_still_goes_whole_and_the_agent_answers(
    tmp_path, earlier, noted
):
    long_text = "Ein langer Satz ohne Ende " * 40
    texts = [*earlier, long_text]
    gateway = FakeGateway(completion("Gut."))

    async def hearing(trace):
        for seq, text in enumerate(texts, start=1):
            yield a_line(seq, text)
            await trace.until(lambda: len(trace.played) == seq)
        await trace.settled()

    place = VoicePlace(tmp_path, window_bytes=len(long_text.encode()))
    assert place.run(hearing, gateway) == RETURNED

    _system, only = messages_of(gateway.requests[-1])
    *opening, line = only["content"].split("\n")
    assert json.loads(line)["text"] == long_text
    assert opening == ([WINDOW_NOTE] if noted else [])
    assert place.trace.played == ["Gut."] * len(texts)


def test_an_agent_without_a_voice_section_is_refused_before_hearing_is_read(tmp_path):
    gateway = FakeGateway(completion("Gut."))

    async def hearing(trace):
        trace.record(("read",))
        yield a_line(1, "Frage?")

    place = VoicePlace(tmp_path, window_bytes=None)
    ending = place.run(hearing, gateway)

    assert isinstance(ending.raised, ValueError)
    assert "voice" in str(ending.raised)
    assert ("read",) not in place.trace.entries
    assert gateway.requests == []


@pytest.mark.parametrize(
    "first_reply", [A_BUDGET_REFUSAL, completion("[shout] Hallo.")], ids=["failed", "unreadable"]
)
def test_lines_a_decision_dropped_stay_dropped_and_the_note_stays_when_it_played_nothing(
    tmp_path, first_reply
):
    gateway = FakeGateway(first_reply, completion("Gut."))

    async def hearing(trace):
        for seq, text in enumerate(QUIET, start=1):
            yield a_line(seq, text, name="Ben")
        yield a_line(len(QUIET) + 1, "Frage?")
        await trace.settled()
        yield a_line(len(QUIET) + 2, "Noch eine Frage?")
        await trace.settled()

    place = VoicePlace(tmp_path, window_bytes=600)
    assert place.run(hearing, gateway, waking={"Frage?", "Noch eine Frage?"}) == RETURNED

    first, second = gateway.requests
    kept = heard_texts(first)
    assert 0 < len(kept) < len(QUIET) + 1
    assert opens_with_the_note(second)
    assert set(heard_texts(second)[:-1]) <= set(kept)
    assert heard_texts(second)[-1] == "Noch eine Frage?"
    assert place.trace.played == ["Gut."]
