"""The closed list of Claude CLI error classes a run may log, and the helper that logs one."""

from __future__ import annotations

import logging
from typing import Final

log = logging.getLogger(__name__)

# The classes a run the CLI ends with an error may log: the assistant line's `error` and the
# result line's `subtype`, as recorded for Claude CLI 2.1.302 (ADR 0001, A1b-0a c4 and the
# limit and max-turns records). Any other value logs as `other`.
_CLI_ERROR_CLASSES: Final = frozenset(
    {"rate_limit", "invalid_request", "error_max_turns", "error_during_execution"}
)
_OTHER_CLI_ERROR_CLASS: Final = "other"


def log_cli_error_class(reported: object) -> None:
    listed = isinstance(reported, str) and reported in _CLI_ERROR_CLASSES
    log.warning(
        "Claude run ended on a CLI error (class=%s)",
        reported if listed else _OTHER_CLI_ERROR_CLASS,
    )
