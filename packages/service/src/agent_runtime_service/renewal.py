"""One renewal attempt of a provider's dedicated login, as one ordered transaction.

An attempt reserves the login file's sibling, reads the source, decides whether it is due, runs
the pinned CLI on a made-due copy in a private home, and only after the child's end is confirmed
reads the home back and commits or abandons. Until the end is confirmed the attempt keeps the
sibling, the pool slot and the home, and blocks every later attempt; a handoff for shutdown is
terminal, so a later confirmation writes and removes nothing. Every attempt ends in one closed
report, logged once, which holds no document content and no CLI output.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Final

from agent_runtime.anchored_turn import CliProcessReservation
from agent_runtime.cli_run import CliRunStart
from agent_runtime.codex.renewal import CodexRenewal, CopyUnchanged
from agent_runtime.codex_login import (
    CodexRenewalRefusal,
    RenewableCodexLogin,
    read_codex_timing,
    renewable_codex_login,
)
from agent_runtime.config import PrivateWorkingRoot
from agent_runtime.login_timing import AccessTokenTiming, UnreadableTiming
from agent_runtime.process import (
    CliEnvironmentUnavailableError,
    CliRunOutcome,
    CliRunReason,
    HolderReservation,
    run_cli_bounded,
    run_directories,
)
from agent_runtime.secret_file import (
    SecretFileFault,
    UnsoundSecretFile,
    read_secret_file,
    shown_path,
)
from agent_runtime.spawn import ChildProcess
from agent_runtime_service.config import LOGIN_READERS, Provider
from agent_runtime_service.login_file import (
    ReservedSwap,
    SiblingHeld,
    SourceChanged,
    SwappedDurabilityUnconfirmed,
    SwapRefused,
    SwapResult,
    reserve_swap,
)

log = logging.getLogger(__name__)

_UNREADABLE_RENEWAL_INTERVAL_SECONDS: Final = 24 * 60 * 60
_CODEX_DEADLINE_SECONDS: Final = 30.0


class RenewalOutcome(StrEnum):
    NOT_DUE = "not due"
    RENEWED = "renewed"
    WRITTEN_NOT_RENEWED = "written, not renewed"
    NOT_RENEWED = "not renewed"
    MALFORMED_RESULT = "malformed result"
    REFRESH_FAILED = "refresh failed"
    READ_BACK_FAILED = "renewed login unreadable"
    NOT_STARTED = "the CLI did not start"
    NO_REFRESH_TOKEN = "no refresh token"  # noqa: S105 - closed outcome text names no secret
    SOURCE_REFUSED = "login file refused"
    SIBLING_HELD = "sibling held"
    RESERVE_REFUSED = "sibling not created"
    POOL_FULL = "pool full"
    HOME_NOT_CREATED = "renewal home not created"
    SOURCE_CHANGED = "source changed"
    COMMIT_REFUSED = "commit refused"
    END_UNCONFIRMED = "end unconfirmed"
    INTERRUPTED = "renewal interrupted with its end unconfirmed"

    @property
    def is_failure(self) -> bool:
        return self not in (RenewalOutcome.NOT_DUE, RenewalOutcome.RENEWED)


class NotRenewedCause(StrEnum):
    TIMING_UNREADABLE = "the renewed timing is unreadable"
    STILL_DUE = "the renewed login is still due"
    REFRESH_TOKEN_UNCHANGED = "refresh token unchanged"  # noqa: S105 - closed cause text names no secret
    EXPIRY_NOT_LATER = "expiry not later"


class Cleanup(StrEnum):
    REMOVED = "removed"
    LEFT = "removal failed"
    RETAINED = "retained"


@dataclass(frozen=True)
class RenewalReport:
    """How one attempt ended; every field is a closed value or a closed text, never content."""

    provider: Provider
    outcome: RenewalOutcome
    cause: NotRenewedCause | SecretFileFault | None = None
    refusal: str | None = None
    source_timing: UnreadableTiming | None = None
    durability_confirmed: bool | None = None
    turn: CliRunReason | None = None
    cleanup: Cleanup | None = None


@dataclass(frozen=True)
class RenewalEntry:
    """What one provider's renewal is: its deadline, its reader and timing, and its CLI run."""

    deadline_seconds: float
    renewable: Callable[[bytes], RenewableCodexLogin | CodexRenewalRefusal]
    timing: Callable[[bytes, int], AccessTokenTiming | UnreadableTiming]
    run: Callable[[Path, RenewableCodexLogin, int], CodexRenewal]


CODEX_RENEWAL: Final = RenewalEntry(
    _CODEX_DEADLINE_SECONDS,
    renewable_codex_login,
    read_codex_timing,
    lambda binary, login, now: CodexRenewal(binary, login, now=now),
)
RENEWALS: Final[Mapping[Provider, RenewalEntry]] = {Provider.OPENAI: CODEX_RENEWAL}


def _judged(
    original: RenewableCodexLogin,
    result: RenewableCodexLogin,
    now: int,
    entry: RenewalEntry,
) -> NotRenewedCause | None:
    """Why a preservable result is not "renewed", or None when it is; the first cause wins."""
    timing = entry.timing(result.document, now)
    if isinstance(timing, UnreadableTiming):
        return NotRenewedCause.TIMING_UNREADABLE
    if timing.is_due(now):
        return NotRenewedCause.STILL_DUE
    if result.shares_refresh_token_with(original):
        return NotRenewedCause.REFRESH_TOKEN_UNCHANGED
    original_timing = entry.timing(original.document, now)
    if (
        isinstance(original_timing, AccessTokenTiming)
        and timing.expires_at <= original_timing.expires_at
    ):
        return NotRenewedCause.EXPIRY_NOT_LATER
    return None


def _refusal_with_remaining_sibling(refusal: str | None, left: SwapRefused) -> str | None:
    if not left.sibling_remains:
        return refusal
    return left.reason if refusal is None else f"{refusal}; {left.reason}"


class _EndConfirmation:
    """The child's confirmed end: it frees the pool slot and nothing else, on any thread."""

    def __init__(self, slot: CliProcessReservation) -> None:
        self._slot = slot
        self._confirmed = threading.Event()
        self.spawn_failed = False

    @property
    def confirmed(self) -> bool:
        return self._confirmed.is_set()

    def reaped(self, _process_id: int, _became_zombie: bool) -> None:
        self._slot.release()
        self._confirmed.set()

    def never_spawned(self) -> None:
        self.spawn_failed = True
        self._slot.release()
        self._confirmed.set()


@dataclass(frozen=True)
class _Pending:
    """An attempt whose end is not settled: everything it still holds."""

    reserved: ReservedSwap
    login: RenewableCodexLogin
    run: CodexRenewal
    home: Path
    holder: HolderReservation
    confirmation: _EndConfirmation
    source_timing: UnreadableTiming | None
    turn: CliRunReason


@dataclass(frozen=True)
class _Prepared:
    """A home and a started child, both still owned by the attempt."""

    home: Path
    holder: HolderReservation
    start: CliRunStart


@dataclass(frozen=True)
class _Settlement:
    outcome: RenewalOutcome
    cause: NotRenewedCause | SecretFileFault | None = None
    refusal: str | None = None
    durability_confirmed: bool | None = None
    retains_home: bool = False


class ProviderRenewer:
    """The one renewer of one provider's login; one owner calls it, one call at a time.

    ``attempt`` and ``hand_over_as_interrupted`` may both commit to the login file, so they are
    called from a worker thread, never the main thread or an event loop.

    Every fact arrives as an argument: no installed configuration is read.
    """

    def __init__(
        self,
        provider: Provider,
        entry: RenewalEntry,
        *,
        binary: Path,
        token_file: Path,
        working_root: PrivateWorkingRoot,
        wall_clock: Callable[[], int],
        monotonic_clock: Callable[[], float],
    ) -> None:
        self._provider = provider
        self._entry = entry
        self._binary = binary
        self._token_file = token_file
        self._working_root = working_root
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._pending: _Pending | None = None
        self._handed_over = False
        self._unreadable_renewal_started: float | None = None

    def attempt(self) -> RenewalReport:
        report = self._attempt()
        self._log(report)
        return report

    def hand_over_as_interrupted(self) -> RenewalReport | None:
        """Settle a confirmed end, or give up an unconfirmed one for good; None if none is held."""
        pending = self._pending
        if pending is None:
            return None
        report = self._attempt()
        if report.outcome is RenewalOutcome.END_UNCONFIRMED:
            self._handed_over = True
            report = self._interrupted(pending)
        self._log(report)
        return report

    def _attempt(self) -> RenewalReport:
        pending = self._pending
        if pending is None:
            return self._fresh()
        if self._handed_over:
            return self._interrupted(pending)
        if not pending.confirmation.confirmed:
            return self._unconfirmed(pending)
        self._pending = None
        return self._settled(pending)

    def _fresh(self) -> RenewalReport:
        reserved = reserve_swap(self._token_file)
        if isinstance(reserved, SiblingHeld):
            return RenewalReport(
                self._provider, RenewalOutcome.SIBLING_HELD, refusal=reserved.reason
            )
        if isinstance(reserved, SwapRefused):
            return RenewalReport(
                self._provider, RenewalOutcome.RESERVE_REFUSED, refusal=reserved.reason
            )
        login = self._source(reserved)
        if isinstance(login, RenewalReport):
            return login
        return self._due_then_run(reserved, login)

    def _source(self, reserved: ReservedSwap) -> RenewableCodexLogin | RenewalReport:
        try:
            document = read_secret_file(self._token_file, "login file")
        except UnsoundSecretFile as refusal:
            return self._abandoned(reserved, RenewalOutcome.SOURCE_REFUSED, refusal=str(refusal))
        login = self._entry.renewable(document)
        if login is CodexRenewalRefusal.NO_REFRESH_TOKEN:
            return self._abandoned(reserved, RenewalOutcome.NO_REFRESH_TOKEN)
        if isinstance(login, CodexRenewalRefusal):
            return self._abandoned(
                reserved,
                RenewalOutcome.SOURCE_REFUSED,
                refusal=f"{shown_path(self._token_file)} {login.value}",
            )
        return login

    def _due_then_run(self, reserved: ReservedSwap, login: RenewableCodexLogin) -> RenewalReport:
        now = self._wall_clock()
        timing = self._entry.timing(login.document, now)
        unreadable = timing if isinstance(timing, UnreadableTiming) else None
        if unreadable is None:
            self._unreadable_renewal_started = None
        if not self._is_due(timing, now):
            return self._abandoned(reserved, RenewalOutcome.NOT_DUE, source_timing=unreadable)
        run = self._entry.run(self._binary, login, now)
        slot = run.pool.reserve()
        if slot is None:
            return self._abandoned(reserved, RenewalOutcome.POOL_FULL, source_timing=unreadable)
        return self._spawned(reserved, login, run, slot, unreadable)

    def _is_due(self, timing: AccessTokenTiming | UnreadableTiming, now: int) -> bool:
        if isinstance(timing, AccessTokenTiming):
            return timing.is_due(now)
        started = self._unreadable_renewal_started
        recent = (
            started is not None
            and self._monotonic_clock() - started < _UNREADABLE_RENEWAL_INTERVAL_SECONDS
        )
        return not recent

    def _spawned(
        self,
        reserved: ReservedSwap,
        login: RenewableCodexLogin,
        run: CodexRenewal,
        slot: CliProcessReservation,
        source_timing: UnreadableTiming | None,
    ) -> RenewalReport:
        confirmation = _EndConfirmation(slot)
        with ExitStack() as tree:
            prepared = self._prepared(tree, run, slot, source_timing)
            if prepared is None:
                return self._abandoned(
                    reserved, RenewalOutcome.HOME_NOT_CREATED, source_timing=source_timing
                )
            outcome = self._bounded(prepared.start.child, prepared.start.stdin, confirmation)
            pending = _Pending(
                reserved,
                login,
                run,
                prepared.home,
                prepared.holder,
                confirmation,
                source_timing,
                outcome.reason,
            )
        if not confirmation.confirmed:
            self._pending = pending
            return self._unconfirmed(pending)
        return self._settled(pending)

    def _prepared(
        self,
        tree: ExitStack,
        run: CodexRenewal,
        slot: CliProcessReservation,
        source_timing: UnreadableTiming | None,
    ) -> _Prepared | None:
        """The home and the child ready to run, or None when no home can be made.

        Until the runner is entered the slot and the home are this method's: whatever fails
        releases both and propagates. From here a source of unreadable timing is held for 24 h.
        """
        holder: HolderReservation | None = None
        try:
            try:
                directories = tree.enter_context(
                    run_directories(self._working_root, run.run_files())
                )
            except CliEnvironmentUnavailableError:
                slot.release()
                return None
            # Released only once the end is confirmed and the home is read: nothing else removes it.
            holder = directories.reserve()
            start = run.start(directories.home, directories.working_directory)
            if source_timing is not None:
                self._unreadable_renewal_started = self._monotonic_clock()
        except BaseException:
            slot.release()
            if holder is not None:
                holder.release()
            raise
        return _Prepared(directories.home, holder, start)

    def _bounded(
        self, child: ChildProcess, stdin: bytes, confirmation: _EndConfirmation
    ) -> CliRunOutcome:
        return run_cli_bounded(
            child,
            stdin_payload=stdin,
            read="all",
            deadline=time.monotonic() + self._entry.deadline_seconds,
            stderr="devnull",
            on_spawn_failed=confirmation.never_spawned,
            on_reaped=confirmation.reaped,
        )

    def _abandoned(
        self,
        reserved: ReservedSwap,
        outcome: RenewalOutcome,
        *,
        refusal: str | None = None,
        source_timing: UnreadableTiming | None = None,
    ) -> RenewalReport:
        left = reserved.abandon()
        return RenewalReport(
            self._provider,
            outcome,
            refusal=_refusal_with_remaining_sibling(refusal, left),
            source_timing=source_timing,
        )

    def _unconfirmed(self, pending: _Pending) -> RenewalReport:
        return self._held(pending, RenewalOutcome.END_UNCONFIRMED)

    def _interrupted(self, pending: _Pending) -> RenewalReport:
        return self._held(pending, RenewalOutcome.INTERRUPTED)

    def _held(self, pending: _Pending, outcome: RenewalOutcome) -> RenewalReport:
        return RenewalReport(
            self._provider,
            outcome,
            source_timing=pending.source_timing,
            turn=pending.turn,
            cleanup=Cleanup.RETAINED,
        )

    def _settled(self, pending: _Pending) -> RenewalReport:
        """After a confirmed end: read the home back, commit or abandon, then release the home."""
        settlement = self._settlement(pending)
        if settlement.retains_home:
            cleanup = Cleanup.RETAINED
        else:
            pending.holder.release()
            cleanup = Cleanup.LEFT if pending.home.parent.exists() else Cleanup.REMOVED
        return RenewalReport(
            self._provider,
            settlement.outcome,
            settlement.cause,
            settlement.refusal,
            pending.source_timing,
            settlement.durability_confirmed,
            pending.turn,
            cleanup,
        )

    def _settlement(self, pending: _Pending) -> _Settlement:
        if pending.confirmation.spawn_failed:
            self._unreadable_renewal_started = None
            return self._abandoning(pending, RenewalOutcome.NOT_STARTED)
        document = pending.run.renewed_document(pending.home)
        if isinstance(document, SecretFileFault):
            return self._abandoning(
                pending, RenewalOutcome.READ_BACK_FAILED, cause=document, retains_home=True
            )
        if isinstance(document, CopyUnchanged):
            return self._abandoning(pending, RenewalOutcome.REFRESH_FAILED)
        return self._judged_result(pending, document)

    def _judged_result(self, pending: _Pending, document: bytes) -> _Settlement:
        if document == pending.login.document:
            return self._abandoning(pending, RenewalOutcome.NOT_RENEWED)
        result = self._entry.renewable(document)
        if isinstance(result, CodexRenewalRefusal):
            return self._abandoning(pending, RenewalOutcome.MALFORMED_RESULT)
        cause = _judged(pending.login, result, self._wall_clock(), self._entry)
        written = pending.reserved.commit(
            document, LOGIN_READERS[self._provider], replaces=pending.login.document
        )
        return self._committed(written, cause)

    def _committed(self, written: SwapResult, cause: NotRenewedCause | None) -> _Settlement:
        if isinstance(written, SourceChanged):
            return _Settlement(RenewalOutcome.SOURCE_CHANGED, refusal=written.reason)
        if isinstance(written, SwapRefused):
            return _Settlement(
                RenewalOutcome.COMMIT_REFUSED, refusal=written.reason, retains_home=True
            )
        durable = not isinstance(written, SwappedDurabilityUnconfirmed)
        outcome = RenewalOutcome.RENEWED if cause is None else RenewalOutcome.WRITTEN_NOT_RENEWED
        return _Settlement(outcome, cause=cause, durability_confirmed=durable)

    def _abandoning(
        self,
        pending: _Pending,
        outcome: RenewalOutcome,
        *,
        cause: SecretFileFault | None = None,
        retains_home: bool = False,
    ) -> _Settlement:
        left = pending.reserved.abandon()
        return _Settlement(
            outcome,
            cause=cause,
            refusal=_refusal_with_remaining_sibling(None, left),
            retains_home=retains_home,
        )

    def _log(self, report: RenewalReport) -> None:
        level = _LOG_LEVELS.get(report.outcome, logging.WARNING)
        log.log(
            level,
            "%s login renewal: %s (cause: %s; source timing: %s; durability confirmed: %s;"
            " turn: %s; cleanup: %s; refusal: %s)",
            report.provider.value,
            report.outcome.value,
            _closed(report.cause),
            _closed(report.source_timing),
            report.durability_confirmed,
            _closed(report.turn),
            _closed(report.cleanup),
            report.refusal,
        )


_LOG_LEVELS: Final = {
    RenewalOutcome.NOT_DUE: logging.DEBUG,
    RenewalOutcome.RENEWED: logging.INFO,
    RenewalOutcome.INTERRUPTED: logging.ERROR,
}


def _closed(value: Enum | None) -> str | None:
    return None if value is None else value.value
