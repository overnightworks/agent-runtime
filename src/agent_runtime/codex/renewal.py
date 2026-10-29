"""The description of one renewal run: the pinned Codex CLI given the full login, made due."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn

from agent_runtime.anchored_turn import CliProcessPool
from agent_runtime.cli_run import CliRunStart
from agent_runtime.cli_settings import CliSettings
from agent_runtime.codex.run import AUTH_FILE_NAME, CodexRun
from agent_runtime.codex_login import RenewableCodexLogin, assess_codex_login, made_due_copy
from agent_runtime.secret_file import SecretFileFault, read_secret_file_without_following_links
from agent_runtime_client.wire import TurnMessage, TurnRole

_RENEWAL_SYSTEM_TEXT: Final = "You answer in one word. You have no tools."
_RENEWAL_REQUEST: Final = (TurnMessage(role=TurnRole.USER, text="Say ok."),)
_FILE_CREDENTIAL_STORE: Final[CliSettings] = {"cli_auth_credentials_store": "file"}


class CopyUnchanged(StrEnum):
    UNCHANGED = "the CLI left the made-due copy as it was"


@dataclass(frozen=True)
class CodexRenewal:
    """One renewal run: the CLI holds the full login, made due, so it renews it and rewrites it.

    It describes the run and starts nothing. Every fact arrives as an argument; no installed
    configuration is read.
    """

    binary: Path
    login: RenewableCodexLogin = field(repr=False)
    now: int = field(kw_only=True)
    _run: CodexRun = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        run = CodexRun(
            self.binary,
            assess_codex_login(self.login.document),
            _RENEWAL_SYSTEM_TEXT,
            _RENEWAL_REQUEST,
            model=None,
            settings=(_FILE_CREDENTIAL_STORE,),
        )
        object.__setattr__(self, "_run", run)

    @property
    def pool(self) -> CliProcessPool:
        return self._run.pool

    def run_files(self) -> Mapping[str, bytes]:
        return {**self._run.run_files(), AUTH_FILE_NAME: self._made_due_copy()}

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        return self._run.start(home, working_directory)

    def renewed_document(self, home: Path) -> bytes | SecretFileFault | CopyUnchanged:
        """What the CLI left as the home's login; the caller checks its structure."""
        read = read_secret_file_without_following_links(home / AUTH_FILE_NAME)
        if isinstance(read, bytes) and read == self._made_due_copy():
            return CopyUnchanged.UNCHANGED
        return read

    def _made_due_copy(self) -> bytes:
        return made_due_copy(self.login, self.now)

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Codex renewal holds credentials and cannot be serialised")
