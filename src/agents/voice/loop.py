"""The voice loop: lines in, the host's wake rule, one decision at a time, sentences out."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from agents.agent_file import AgentDefinition, TurnRequest
from agents.backend import AgentBackend, TurnPerson, TurnStopped
from agents.member import PlaceEntry
from agents.voice import (
    DecisionFailed,
    EchoDropped,
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
from agents.voice.conversation import Conversation
from agents.voice.echo import is_echo
from agents.voice.reply import PROTOCOL_TEXT, SILENT_REPLY, ReplyReader


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

    Every line enters one running conversation, but the agent's own spoken line and an echo: a
    spoken line that repeats the agent's latest answer (``voice.echo.is_echo``) is reported as
    ``EchoDropped`` and never asked of ``wakes``. An act line enters the conversation and is
    never asked of ``wakes`` either; it does not count for ``first_after_answer``. Any other
    line that is not the agent's own and that ``wakes`` accepts starts a decision; at most one
    is in flight and one answer plays, and a line that wakes meanwhile gets exactly one decision
    after it, over every line since the last one. The system text is the agent's prompt, the
    protocol text and ``context``. A decision sends at most the agent's voice window of the
    conversation; only the newest line goes beyond it, alone and behind the window note once
    anything went. An agent without a voice section raises ``ValueError`` before ``hearing`` is
    read.

    Each decision's turn is read to its end in its own task while its sentences play. They play
    in written order, and the next one renders while one plays, never more than one ahead. A
    ``VoiceRefused`` ends the answer: the turn is closed and the queued sentences dropped, a
    rendered one included; a render refused while the sentence before it plays is reported once
    that one has played. A ``TurnStopped`` is reported as ``DecisionFailed``, and the sentences
    already queued play out. A decision that played nothing and was not silent leaves its lines
    undecided until a line wakes the agent again.

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

    played: list[str] = field(default_factory=list[str])
    playing: str | None = None
    openings: list[StayedSilent | ReplyUnreadable] = field(
        default_factory=list[StayedSilent | ReplyUnreadable]
    )
    ended_short: bool = False

    def started(self) -> list[str]:
        """Every sentence that started playing and was not refused, the one playing included."""
        return self.played if self.playing is None else [*self.played, self.playing]

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
        self._answer = _Answer()
        self._last_played: list[str] = []

    def hear(self, line: PlaceEntry) -> None:
        if is_echo(line, self._latest_answer()):
            self._conversation.skip(line)
            self._report(EchoDropped(line.text))
            return
        self._conversation.hear(line)
        if line.own or line.act:
            return
        first_after_answer, self._first_after_answer = self._first_after_answer, False
        if self._wakes(line, first_after_answer=first_after_answer):
            self._woken.set()

    def _latest_answer(self) -> list[str]:
        """What the most recent decision that played at least one sentence has started."""
        return self._answer.started() or self._last_played

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
        answer = self._answer = _Answer()
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        reading = self._group.create_task(self._read(request, sentences, answer))
        try:
            await self._play(sentences, answer)
        finally:
            reading.cancel()
            await asyncio.wait({reading})
        self._conversation.close_decision(answer.recorded())
        if answer.played:
            self._last_played = answer.played
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
        sentences.put_nowait(None)

    async def _play(self, sentences: asyncio.Queue[str | None], answer: _Answer) -> None:
        """Play the sentences in written order; the next one renders while one plays."""
        ahead = self._group.create_task(self._render_next(sentences))
        try:
            while (rendered := await ahead) is not None:
                if isinstance(rendered, SentenceRefused):
                    refused = rendered
                else:
                    ahead = self._group.create_task(self._render_next(sentences))
                    refused = await self._play_one(*rendered, answer)
                if refused is not None:
                    answer.ended_short = True
                    self._report(refused)
                    return
        finally:
            ahead.cancel()
            await asyncio.wait({ahead})

    async def _render_next(
        self, sentences: asyncio.Queue[str | None]
    ) -> tuple[str, Audio] | SentenceRefused | None:
        """The next sentence and its sound, the refusal of its render, or ``None`` at the end."""
        if (sentence := await sentences.get()) is None:
            return None
        try:
            return sentence, await self._voice.render(sentence)
        except VoiceRefused:
            return SentenceRefused(sentence)

    async def _play_one(
        self, sentence: str, audio: Audio, answer: _Answer
    ) -> SentenceRefused | None:
        """Play one rendered sentence; the refusal of its play, or ``None`` once it ended."""
        answer.playing = sentence
        self._report(SentenceStarted(sentence))
        try:
            await self._voice.play(audio)
        except VoiceRefused:
            return SentenceRefused(sentence)
        finally:
            answer.playing = None
        answer.played.append(sentence)
        return None
