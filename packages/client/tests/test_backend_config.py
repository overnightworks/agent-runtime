"""The ``agent`` section of an app's configuration and the gateway it names."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_runtime_client.config import AgentConfig

GATEWAY = {"origin": "http://gateway.test:4000", "key_file": "/run/secrets/key"}
HTTPS_GATEWAY = {
    "origin": "https://gateway.test",
    "key_file": "/run/secrets/key",
    "ca_file": "/run/secrets/ca.pem",
}
SECTION = {"backend": "litellm", "gateway": GATEWAY, "api_group": "/apps/songmaker"}


def test_c7_the_litellm_backend_with_a_gateway_and_group_builds():
    config = AgentConfig.model_validate(SECTION)

    assert config.gateway.origin == "http://gateway.test:4000"
    assert config.api_group == "/apps/songmaker"


@pytest.mark.parametrize(
    "gateway", [GATEWAY, HTTPS_GATEWAY, {**GATEWAY, "origin": "http://[::1]:80"}]
)
def test_c7_a_http_or_https_origin_builds(gateway):
    assert AgentConfig.model_validate({**SECTION, "gateway": gateway}).gateway.origin


@pytest.mark.parametrize(
    "section",
    [
        pytest.param({**SECTION, "backend": "service"}, id="service-backend"),
        pytest.param({**SECTION, "backend": "x"}, id="unknown-backend"),
        pytest.param({**SECTION, "backend": None}, id="null-backend"),
        pytest.param({**SECTION, "service_url": "http://agent.test"}, id="old-service-url"),
        pytest.param({**SECTION, "workload_scope": "agent"}, id="old-workload-scope"),
        pytest.param({**SECTION, "api_group": ""}, id="empty-api-group"),
    ],
)
def test_c8_a_malformed_section_fails_loud(section):
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(section)


@pytest.mark.parametrize("missing", ["backend", "gateway", "api_group"])
def test_c8_a_missing_host_fact_fails_loud(missing):
    section = {name: value for name, value in SECTION.items() if name != missing}

    with pytest.raises(ValidationError, match=missing):
        AgentConfig.model_validate(section)


@pytest.mark.parametrize(
    "origin",
    [
        "http://gateway.test/v1",
        "http://gateway.test/",
        "http://gateway.test?x=1",
        "http://gateway.test#x",
        "http://user:pass@gateway.test",
        "http://gateway.test:0",
        "http://gateway.test:65536",
        "http://[not-an-address]",
        "http://gäteway.test",
        "gateway.test",
        "ftp://gateway.test",
        "",
    ],
)
def test_c9_a_malformed_origin_fails_loud(origin):
    with pytest.raises(ValidationError, match="origin"):
        AgentConfig.model_validate({**SECTION, "gateway": {**GATEWAY, "origin": origin}})


@pytest.mark.parametrize(
    ("gateway", "field"),
    [
        pytest.param({**GATEWAY, "origin": "https://gateway.test"}, "ca_file", id="https-no-ca"),
        pytest.param({**GATEWAY, "ca_file": "/run/secrets/ca.pem"}, "ca_file", id="http-with-ca"),
        pytest.param({**GATEWAY, "key_file": "key"}, "key_file", id="relative-key"),
        pytest.param({**HTTPS_GATEWAY, "ca_file": "ca.pem"}, "ca_file", id="relative-ca"),
        pytest.param({**GATEWAY, "key_file": "~/key"}, "key_file", id="tilde-key"),
        pytest.param({**GATEWAY, "key_file": 5}, "key_file", id="numeric-key"),
        pytest.param({**GATEWAY, "extra": 1}, "extra", id="unknown-key"),
    ],
)
def test_c10_a_malformed_file_setting_fails_loud(gateway, field):
    with pytest.raises(ValidationError, match=field):
        AgentConfig.model_validate({**SECTION, "gateway": gateway})
