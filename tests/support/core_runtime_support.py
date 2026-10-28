"""The autouse fixture of the service modules that run with no installed core configuration."""

from __future__ import annotations

import pytest

from agent_runtime.config import reset_config


@pytest.fixture(autouse=True)
def no_installed_core_runtime() -> None:
    reset_config()
