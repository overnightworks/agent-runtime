"""The description of one renewal run: the pinned Grok CLI given the full login, made due."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, NoReturn

from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.cli_run import CliRunStart
from agent_runtime.grok.run import (
    AUTH_FILE_NAME,
    GROK_CLI_PROCESS_POOL,
    GROK_HOME_VARIABLE,
    GrokStreamDecoder,
)
from agent_runtime.grok_login import RenewableGrokLogin, read_grok_timing, renewable_grok_login
from agent_runtime.login_renewal import CopyUnchanged, RenewableLogin
from agent_runtime.login_timing import AccessTokenTiming
from agent_runtime.secret_file import SecretFileFault, read_secret_file_without_following_links
from agent_runtime.spawn import ChildProcess, ChildProcessSpecificationError, closed_environment

_EARLY_INVALIDATION_VARIABLE: Final = "GROK_AUTH_EARLY_INVALIDATION_SECS"
_RENEWAL_ARGUMENTS: Final = ("models",)
_MARGIN_PAST_EXPIRY_SECONDS: Final = 3600
_UNREADABLE_TIMING_SECONDS: Final = 24 * 3600


@dataclass(frozen=True)
class GrokRenewal:
    """One renewal run: the CLI holds the full login and is told it is due, so it renews it.

    It describes the run and starts nothing. Every fact arrives as an argument; no installed
    configuration is read.
    """

    binary: Path
    login: RenewableLogin = field(repr=False)
    now: int = field(kw_only=True)

    def __post_init__(self) -> None:
        if not self.binary.is_absolute():
            raise ChildProcessSpecificationError("A Grok renewal needs an absolute binary")
        if not (
            isinstance(self.login, RenewableGrokLogin)
            and isinstance(renewable_grok_login(self.login.document), RenewableGrokLogin)
        ):
            raise ChildProcessSpecificationError("A Grok renewal needs a renewable Grok login")

    @property
    def pool(self) -> CliProcessPool:
        return GROK_CLI_PROCESS_POOL

    def run_files(self) -> Mapping[str, bytes]:
        return {AUTH_FILE_NAME: self.login.document}

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        child = ChildProcess(
            binary=self.binary,
            arguments=_RENEWAL_ARGUMENTS,
            environment=closed_environment(
                home,
                **{
                    GROK_HOME_VARIABLE: str(home),
                    _EARLY_INVALIDATION_VARIABLE: str(self._early_invalidation_seconds()),
                },
            ),
            working_directory=working_directory,
        )
        return CliRunStart(child, b"", GrokStreamDecoder(relay_attached=False))

    def renewed_document(self, home: Path) -> bytes | SecretFileFault | CopyUnchanged:
        """What the CLI left as the home's login; the caller checks its structure."""
        read = read_secret_file_without_following_links(home / AUTH_FILE_NAME)
        if isinstance(read, bytes) and read == self.login.document:
            return CopyUnchanged.UNCHANGED
        return read

    def _early_invalidation_seconds(self) -> int:
        timing = read_grok_timing(self.login.document, self.now)
        if isinstance(timing, AccessTokenTiming):
            return max(timing.expires_at - self.now, 0) + _MARGIN_PAST_EXPIRY_SECONDS
        return _UNREADABLE_TIMING_SECONDS

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Grok renewal holds credentials and cannot be serialised")
