"""The provider-neutral contract of a login renewal: the login, its run and the refusals."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agent_runtime.anchored_turn import CliProcessPool
    from agent_runtime.cli_run import CliRunStart
    from agent_runtime.secret_file import SecretFileFault


class RenewalRefusal(StrEnum):
    NOT_USABLE = "the document is not a usable login"
    NO_REFRESH_TOKEN = "the login holds no refresh token"  # noqa: S105 - closed cause text names no secret


class CopyUnchanged(StrEnum):
    UNCHANGED = "the CLI left the made-due copy as it was"


class RenewableLogin(Protocol):
    """A usable login that still holds its refresh token."""

    @property
    def document(self) -> bytes: ...

    def shares_refresh_token_with(self, other: Self) -> bool: ...


class RenewalRun(Protocol):
    """One renewal run: it describes the CLI run and starts nothing itself."""

    @property
    def pool(self) -> CliProcessPool: ...

    def run_files(self) -> Mapping[str, bytes]: ...

    def start(self, home: Path, working_directory: Path) -> CliRunStart: ...

    def renewed_document(self, home: Path) -> bytes | SecretFileFault | CopyUnchanged: ...
