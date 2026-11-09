"""What the voice loop's tests share: lines, a trace, a recording voice, a paced gateway and a run.

Every run drives ``run_voice_loop`` with the real ``GatewayBackend`` over a scripted transport.
The trace holds, in order, what the loop reported, what it called on the voice, what it asked
the wake rule and what the paced gateway saw, so a test waits on and asserts outcomes only.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx2
import pytest

from agent_runtime_client.agents import AgentDefinition
from agent_runtime_client.backend.gateway import GatewayBackend
from agent_runtime_client.member import PlaceEntry
from agent_runtime_client.tools import HandTool
from agent_runtime_client.voice import VoiceEvent, WakeRule
from agent_runtime_client.voice.loop import run_voice_loop
from tests.support.gateway_turn_support import a_chunk, a_gateway_config, a_person, an_event_stream
from tests.support.virtual_time import VirtualTimeLoop

PROMPT: Final = "Du hörst zu und antwortest kurz."
CONTEXT: Final = "Im Raum sind Ada und Ben."
_SETTLE_SECONDS: Final = 1.0
_REAL_TIME_BOUND_SECONDS: Final = 10.0
_VOICE_EVENTS: Final = VoiceEvent.__value__

type Wait = Callable[[], Awaitable[object]]
type StreamPart = bytes | Wait | Exception


def a_line(
    seq: int, text: str, *, name: str = "Ada", kind: str = "person", own=False, spoken=True
) -> PlaceEntry:
    return PlaceEntry(seq=seq, kind=kind, name=name, text=text, own=own, spoken=spoken)


def piece(text: str) -> bytes:
    """One content chunk of a streamed round."""
    return an_event_stream(a_chunk(text), done=False)


def finish(reason: str = "stop") -> bytes:
    """The finish chunk and ``[DONE]``."""
    return an_event_stream(a_chunk(finish_reason=reason))


async def hang() -> None:
    await asyncio.Event().wait()


class Trace:
    """Everything the loop did that a test may see, in order, and a way to wait for a state.

    Voice events are recorded as reported; the support's own entries are tuples that start
    with their kind: ``render``, ``play``, ``played``, ``stopped``, ``asked``, ``tool``,
    ``closed`` and ``cancelled``.
    """

    def __init__(self) -> None:
        self.entries: list[object] = []
        self.loop_task: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()

    def record(self, entry: object) -> None:
        self.entries.append(entry)
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def until(self, holds: Callable[[], bool]) -> None:
        while not holds():
            await self._changed.wait()

    async def settled(self) -> None:
        """Return once every other task waits; the virtual clock moves only then."""
        await asyncio.sleep(_SETTLE_SECONDS)

    async def host_cancels(self) -> None:
        """The host cancels the loop; the hearing that awaits this is cancelled with it."""
        assert self.loop_task is not None
        self.record(("cancelled",))
        self.loop_task.cancel()
        await hang()

    @property
    def events(self) -> list[object]:
        return [entry for entry in self.entries if isinstance(entry, _VOICE_EVENTS)]

    @property
    def calls(self) -> list[tuple[str, str]]:
        kinds = {"render", "play", "played", "stopped"}
        return [entry for entry in self.entries if isinstance(entry, tuple) and entry[0] in kinds]

    @property
    def asked(self) -> list[tuple[str, bool]]:
        return [entry[1:] for entry in self.entries if _kind_of(entry) == "asked"]

    @property
    def played(self) -> list[str]:
        return [entry[1] for entry in self.entries if _kind_of(entry) == "played"]

    def count(self, entry: object) -> int:
        return self.entries.count(entry)

    def events_after(self, mark: object) -> list[object]:
        """The voice events recorded after ``mark`` first was."""
        later = self.entries[self.entries.index(mark) + 1 :]
        return [entry for entry in later if isinstance(entry, _VOICE_EVENTS)]


def _kind_of(entry: object) -> str | None:
    return entry[0] if isinstance(entry, tuple) else None


class RecordingVoice:
    """A ``VoiceOut`` whose sound is the sentence itself; it records each call in the trace.

    ``raises`` maps a call and its sentence to what that call raises, and a call in ``hangs``
    never returns. A sentence in ``held`` plays until ``release`` ends it, and every play takes
    ``seconds_per_sentence`` on the loop's clock.
    """

    def __init__(
        self,
        trace: Trace,
        *,
        raises: dict[tuple[str, str], Exception] | None = None,
        hangs: Collection[tuple[str, str]] = (),
        held: Collection[str] = (),
        seconds_per_sentence: float = 0.0,
    ) -> None:
        self._trace = trace
        self._raises = raises or {}
        self._hangs = hangs
        self._held = {sentence: asyncio.Event() for sentence in held}
        self._seconds = seconds_per_sentence

    def release(self, sentence: str) -> None:
        self._held[sentence].set()

    async def render(self, sentence: str) -> str:
        await self._called("render", sentence)
        return sentence

    async def play(self, audio: str) -> None:
        try:
            await self._called("play", audio)
            if audio in self._held:
                await self._held[audio].wait()
            if self._seconds:
                await asyncio.sleep(self._seconds)
        except asyncio.CancelledError:
            self._trace.record(("stopped", audio))
            raise
        self._trace.record(("played", audio))

    async def _called(self, call: str, sentence: str) -> None:
        self._trace.record((call, sentence))
        if (call, sentence) in self._raises:
            raise self._raises[call, sentence]
        if (call, sentence) in self._hangs:
            await hang()


class PacedGateway(httpx2.AsyncBaseTransport):
    """Answers each request with its next streamed reply, the last one repeating.

    A reply is a sequence of parts: event-stream bytes, sent as one chunk; a wait, awaited before
    the next part; or an exception, which breaks the stream. The trace records ``closed`` when a
    reply's body is closed, whether it ended or the turn was closed.
    """

    def __init__(self, trace: Trace, *replies: Sequence[StreamPart]) -> None:
        self._trace = trace
        self._replies = list(replies)
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self.requests.append(request)
        parts = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        headers = {"content-type": "text/event-stream"}
        return httpx2.Response(200, headers=headers, stream=_PacedBody(parts, self._trace))


class _PacedBody(httpx2.AsyncByteStream):
    def __init__(self, parts: Sequence[StreamPart], trace: Trace) -> None:
        self._parts = parts
        self._trace = trace

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self._parts:
            if isinstance(part, bytes):
                yield part
            elif isinstance(part, Exception):
                raise part
            else:
                await part()

    async def aclose(self) -> None:
        self._trace.record(("closed",))


async def _stream_one_reply(
    parts: Sequence[StreamPart], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Answer one request with ``parts`` as one chunked event stream, then close."""
    head = await reader.readuntil(b"\r\n\r\n")
    length = re.search(rb"content-length: *(\d+)", head, re.IGNORECASE)
    await reader.readexactly(int(length[1]) if length else 0)
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    )
    for part in parts:
        if isinstance(part, bytes):
            writer.write(b"%x\r\n%s\r\n" % (len(part), part))
            await writer.drain()
        elif isinstance(part, Exception):
            raise part
        else:
            await part()
    writer.write(b"0\r\n\r\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def messages_of(request: httpx2.Request) -> list[dict]:
    """The messages a request sent the gateway: the system message first."""
    return json.loads(request.content)["messages"]


def rows_of(message: dict) -> list[dict]:
    """The lines of a user message, one JSON object each."""
    return [json.loads(row) for row in message["content"].splitlines()]


@dataclass(frozen=True)
class Ending:
    """How the loop ended: returned, raised, or cancelled by the host."""

    raised: BaseException | None = None
    cancelled: bool = False


RETURNED: Final = Ending()

type Hearing = Callable[[Trace], AsyncIterator[PlaceEntry]]


@dataclass
class VoicePlace:
    """The agent, its person and gateway configuration, and the trace of one loop run."""

    directory: Path
    trace: Trace = field(default_factory=Trace)

    def agent(self, tools: Sequence[HandTool] = ()) -> AgentDefinition:
        return AgentDefinition(
            name="listener",
            description="Hears a conversation and answers by voice.",
            roles=["admin", "user"],
            tools=tuple(tool.declaration for tool in tools),
            prompt=PROMPT,
            model="text/standin",
            max_duration_seconds=60,
            max_tool_calls=3 if tools else None,
        )

    def run(
        self,
        hearing: Hearing,
        transport: httpx2.AsyncBaseTransport,
        *,
        waking: Collection[str] | None = None,
        voice: RecordingVoice | None = None,
        tools: Sequence[HandTool] = (),
    ) -> Ending:
        """Run the loop to its end in virtual time; ``waking`` names the texts that wake.

        With no ``waking``, every line wakes.
        """
        config = a_gateway_config(self.directory)
        backend = GatewayBackend(config, tools=tools, transport=transport)
        with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
            return runner.run(self._ended(hearing, backend, waking, voice, tools))

    def run_over_a_socket(self, hearing: Hearing, parts: Sequence[StreamPart]) -> Ending:
        """Run the loop on the wall clock against a loopback gateway that streams ``parts``."""

        async def main() -> Ending:
            server = await asyncio.start_server(
                functools.partial(_stream_one_reply, parts), "127.0.0.1", 0
            )
            port = server.sockets[0].getsockname()[1]
            config = a_gateway_config(self.directory)
            gateway = config.gateway.model_copy(update={"origin": f"http://127.0.0.1:{port}"})
            backend = GatewayBackend(config.model_copy(update={"gateway": gateway}))
            async with server:
                try:
                    return await self._ended(hearing, backend, None, None, ())
                finally:
                    server.close_clients()

        return asyncio.run(asyncio.wait_for(main(), _REAL_TIME_BOUND_SECONDS))

    async def _ended(
        self,
        hearing: Hearing,
        backend: GatewayBackend,
        waking: Collection[str] | None,
        voice: RecordingVoice | None,
        tools: Sequence[HandTool],
    ) -> Ending:
        loop = asyncio.create_task(
            run_voice_loop(
                self.agent(tools),
                backend,
                a_person(),
                hearing(self.trace),
                RecordingVoice(self.trace) if voice is None else voice,
                wakes=self._wake_rule(waking),
                context=CONTEXT,
                report=self.trace.record,
            )
        )
        self.trace.loop_task = loop
        try:
            await asyncio.wait({loop})
        finally:
            loop.cancel()
        if loop.cancelled():
            return Ending(cancelled=True)
        return Ending(raised=loop.exception())

    def _wake_rule(self, waking: Collection[str] | None) -> WakeRule:
        def wakes(line: PlaceEntry, *, first_after_answer: bool) -> bool:
            self.trace.record(("asked", line.text, first_after_answer))
            return waking is None or line.text in waking

        return wakes


@pytest.fixture
def place(tmp_path) -> VoicePlace:
    return VoicePlace(tmp_path)
