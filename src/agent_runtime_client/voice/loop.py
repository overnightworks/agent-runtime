"""The voice loop: lines in, the host's wake rule, one decision at a time, sentences out."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Final

from agent_runtime_client.agents import AgentDefinition, TurnRequest
from agent_runtime_client.backend import AgentBackend, TurnPerson, TurnStopped
from agent_runtime_client.member import PlaceEntry
from agent_runtime_client.voice import (
    DecisionFailed,
    ReplyUnreadable,
    SentenceRefused,
    SentenceStarted,
    StayedSilent,
    VoiceEvent,
    VoiceOut,
    VoiceRefused,
    WakeRule,
    Woke,
)
from agent_runtime_client.voice.conversation import Conversation
from agent_runtime_client.voice.reply import PROTOCOL_TEXT, SILENT_REPLY, ReplyReader

_SENTENCES_END: Final = None


async def run_voice_loop[Audio](
    agent: AgentDefinition,
    backend: AgentBackend,
    person: TurnPerson,
    hearing: AsyncIterator[PlaceEntry],
    voice: VoiceOut[Audio],
    *,
    wakes: WakeRule,
    context: str,
    report: Callable[[VoiceEvent], None],
) -> None:
    """Hear a place's lines and answer by voice until hearing ends.

    Every line enters one running conversation, but the agent's own spoken line. A line that is
    not the agent's own and that ``wakes`` accepts starts a decision; at most one is in flight
    and one answer plays, and a line that wakes meanwhile gets exactly one decision after it,
    over every line since the last one. The system text is the agent's prompt, the protocol
    text and ``context``. A decision sends at most the agent's voice window of the conversation,
    but a lone newest line larger than the window goes whole; an agent without a voice section
    raises ``ValueError`` before ``hearing`` is read.

    Each decision's turn is read to its end in its own task while its sentences play. A
    ``VoiceRefused`` ends the answer: the turn is closed and the queued sentences dropped. A
    ``TurnStopped`` is reported as ``DecisionFailed``, and the sentences already queued play
    out. A decision that played nothing and was not silent leaves its lines undecided until a
    line wakes the agent again.

    When hearing ends, the loop closes the turn, stops the sound and returns. Any other error
    of a port or the backend, and a ``seq`` not above the last, closes the turn, stops the
    sound and is raised inside an ``ExceptionGroup``. On the host's cancel nothing more is
    reported. A port that never returns holds the answer while hearing goes on.
    """
    if agent.voice_window_bytes is None:
        raise ValueError(f"agent {agent.name}: the voice loop needs a voice section")
    conversation = Conversation(agent.voice_window_bytes)
    async with asyncio.TaskGroup() as group:
        loop = _VoiceLoop(
            agent, backend, person, conversation, voice, wakes, context, report, group
        )
        answering = group.create_task(loop.answer_when_woken())
        async for line in hearing:
            loop.hear(line)
        answering.cancel()


@dataclass
class _Answer:
    """What one decision played and how its reply read."""

    played: list[str] = field(default_factory=list)
    openings: list[StayedSilent | ReplyUnreadable] = field(default_factory=list)
    ended_short: bool = False

    def recorded(self) -> str | None:
        """What the conversation records, or ``None`` when the lines stay undecided."""
        if self.played:
            return " ".join(self.played)
        silent = self.openings and all(isinstance(read, StayedSilent) for read in self.openings)
        return SILENT_REPLY if silent and not self.ended_short else None


class _VoiceLoop[Audio]:
    def __init__(
        self,
        agent: AgentDefinition,
        backend: AgentBackend,
        person: TurnPerson,
        conversation: Conversation,
        voice: VoiceOut[Audio],
        wakes: WakeRule,
        context: str,
        report: Callable[[VoiceEvent], None],
        group: asyncio.TaskGroup,
    ) -> None:
        self._agent = agent
        self._backend = backend
        self._person = person
        self._voice = voice
        self._wakes = wakes
        self._report = report
        self._group = group
        self._appended_system = "\n\n".join(part for part in (PROTOCOL_TEXT, context) if part)
        self._conversation = conversation
        self._woken = asyncio.Event()
        self._first_after_answer = False

    def hear(self, line: PlaceEntry) -> None:
        self._conversation.hear(line)
        if line.own:
            return
        first_after_answer, self._first_after_answer = self._first_after_answer, False
        if self._wakes(line, first_after_answer=first_after_answer):
            self._woken.set()

    async def answer_when_woken(self) -> None:
        while True:
            await self._woken.wait()
            self._woken.clear()
            self._first_after_answer = False
            played = await self._decide()
            self._first_after_answer = played

    async def _decide(self) -> bool:
        """Run one decision to its end; whether it played at least one sentence."""
        self._report(Woke())
        messages = self._conversation.open_decision()
        request = self._agent.request(messages, appended_system=self._appended_system)
        answer = _Answer()
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        reading = self._group.create_task(self._read(request, sentences, answer))
        try:
            await self._play(sentences, answer)
        finally:
            reading.cancel()
            await asyncio.wait({reading})
        self._conversation.close_decision(answer.recorded())
        return bool(answer.played)

    async def _read(
        self, request: TurnRequest, sentences: asyncio.Queue[str | None], answer: _Answer
    ) -> None:
        reader = ReplyReader()
        try:
            async with contextlib.aclosing(self._backend.run(request, self._person)) as events:
                async for event in events:
                    reading = reader.read(event)
                    if reading.opening is not None:
                        answer.openings.append(reading.opening)
                        self._report(reading.opening)
                    for sentence in reading.sentences:
                        sentences.put_nowait(sentence)
        except TurnStopped as stopped:
            answer.ended_short = True
            self._report(DecisionFailed(stopped.kind, stopped.code, stopped.detail))
        sentences.put_nowait(_SENTENCES_END)

    async def _play(self, sentences: asyncio.Queue[str | None], answer: _Answer) -> None:
        while (sentence := await sentences.get()) is not _SENTENCES_END:
            try:
                audio = await self._voice.render(sentence)
                self._report(SentenceStarted(sentence))
                await self._voice.play(audio)
            except VoiceRefused:
                answer.ended_short = True
                self._report(SentenceRefused(sentence))
                return
            answer.played.append(sentence)
