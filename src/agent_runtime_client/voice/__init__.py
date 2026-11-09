"""A conversation by voice, as text: the ports a host passes in and the events the loop reports.

``agent_runtime_client.voice.loop.run_voice_loop`` hears a place's lines, lets the host's
``WakeRule`` decide which line starts a decision, and speaks the agent's answer sentence by
sentence through the host's ``VoiceOut``. Sound never reaches this package: the hearing port
yields ``PlaceEntry`` text and ``VoiceOut`` takes sentences. It needs the ``voice`` extra, and
no base module imports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_runtime_client.backend import TurnStopKind
from agent_runtime_client.member import PlaceEntry
from agent_runtime_client.turn import FailureCode, RejectionCode

__all__ = [
    "DecisionFailed",
    "EchoDropped",
    "ReplyUnreadable",
    "SentenceRefused",
    "SentenceStarted",
    "StayedSilent",
    "VoiceEvent",
    "VoiceOut",
    "VoiceRefused",
    "WakeRule",
    "Woke",
]


class VoiceRefused(Exception):
    """The host cannot speak this sentence now; the answer ends."""


class VoiceOut[Audio](Protocol):
    """How the host speaks one sentence: render it, then play what it rendered.

    The loop plays the sentences in order: ``play`` for a sentence starts only after it returned
    for the one before. While one sentence plays, the loop renders the next, never more than one
    ahead, so ``render`` may run while ``play`` does.
    """

    async def render(self, sentence: str) -> Audio:
        """The sentence's sound; it may queue the sound where the app plays in order."""

    async def play(self, audio: Audio) -> None:
        """Return when the sound has ended; cancelling stops it at once."""


class WakeRule(Protocol):
    """Whether a line that is neither the agent's own nor an echo starts a decision.

    ``first_after_answer`` is true for the first such line heard after a decision that played
    at least one sentence ended, while no decision is in flight; it is false for every other
    line, a line heard while an answer plays included.
    """

    def __call__(self, line: PlaceEntry, *, first_after_answer: bool) -> bool: ...


@dataclass(frozen=True)
class Woke:
    """A decision starts."""


@dataclass(frozen=True)
class StayedSilent:
    """A round of the reply opened with ``[silent]``."""


@dataclass(frozen=True)
class ReplyUnreadable:
    """A round of the reply opened with an unknown or unclosed marker; none of it is spoken."""


@dataclass(frozen=True)
class SentenceStarted:
    """A sentence starts playing: the loop hands its sound to ``VoiceOut.play``."""

    text: str


@dataclass(frozen=True)
class SentenceRefused:
    """``VoiceRefused`` ended the answer at this sentence."""

    text: str


@dataclass(frozen=True)
class DecisionFailed:
    """The decision's turn ended short with ``TurnStopped``; its kind, code and detail."""

    kind: TurnStopKind
    code: RejectionCode | FailureCode
    detail: str | None


@dataclass(frozen=True)
class EchoDropped:
    """A spoken line repeated the agent's latest answer: it never enters and wakes nothing."""

    text: str


type VoiceEvent = (
    Woke
    | StayedSilent
    | ReplyUnreadable
    | SentenceStarted
    | SentenceRefused
    | DecisionFailed
    | EchoDropped
)
"""What the loop reports; a host may show, log or measure it. An in-process record."""
