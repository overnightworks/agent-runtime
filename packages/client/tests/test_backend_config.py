"""The ``agent`` section of an app's configuration and the constants ``ServiceBackend`` reads."""

from __future__ import annotations

import pytest
from annotated_types import Le
from pydantic import ValidationError

from agent_runtime_client.backend.service import (
    CONNECT_TIMEOUT_S,
    OTHER_TIMEOUT_S,
    READ_TIMEOUT_S,
    ServiceBackend,
)
from agent_runtime_client.config import AgentConfig
from agent_runtime_client.wire import RunStarted

SECTION = {"backend": "service", "service_url": "http://agent.test", "workload_scope": "agent"}


def test_m1_the_service_backend_is_built_from_configuration():
    config = AgentConfig.model_validate(SECTION)

    assert ServiceBackend(config) is not None


def test_m2_the_litellm_backend_fails_loud_until_this_release_includes_it():
    with pytest.raises(ValidationError, match="`litellm` needs agent-runtime's chat-completions"):
        AgentConfig.model_validate({**SECTION, "backend": "litellm"})


@pytest.mark.parametrize("backend", ["x", None, 1])
def test_m3_a_malformed_backend_fails_loud(backend):
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**SECTION, "backend": backend})


@pytest.mark.parametrize("missing", ["backend", "service_url", "workload_scope"])
def test_m4_a_missing_host_fact_fails_loud(missing):
    section = {name: value for name, value in SECTION.items() if name != missing}

    with pytest.raises(ValidationError, match=missing):
        AgentConfig.model_validate(section)


def test_m37_the_read_timeout_outlasts_the_largest_heartbeat_the_wire_admits():
    (largest,) = [
        bound.le
        for bound in RunStarted.model_fields["heartbeat_interval_s"].metadata
        if isinstance(bound, Le)
    ]

    assert READ_TIMEOUT_S > largest
    assert READ_TIMEOUT_S > OTHER_TIMEOUT_S > CONNECT_TIMEOUT_S
