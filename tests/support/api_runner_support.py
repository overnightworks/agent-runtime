"""The configuration, canned answer and fake gateway the service's API-route tests share."""

from __future__ import annotations

import httpx
from tests.support.api_support import write_api_credentials
from tests.support.service_support import LISTED_MODEL, service_config

from agent_runtime_service import api_runner
from agent_runtime_service.cli_settings_config import CliSettingsConfig
from agent_runtime_service.config import DefaultsConfig, Provider, ServiceConfig


def configured(tmp_path, **api):
    path = write_api_credentials(tmp_path / "gateway.json", {"shared_key": "synthetic-gateway-key"})
    base = service_config()
    return ServiceConfig.model_validate({
        **{name: getattr(base, name) for name in ServiceConfig.model_fields},
        "api": {"completion_url": "http://gateway/v1/chat/completions",
                "credential_file": str(path), **api},
    })


CONFIGURED_MODEL = f"{Provider.ANTHROPIC}/{LISTED_MODEL}"


def with_default_model(config, model):
    return config.model_copy(update={"defaults": DefaultsConfig(model=model)})


def with_cli_settings(config, settings):
    """``config`` with the same ``openai`` and ``xai`` tables at ``[defaults]`` and every app."""
    table = CliSettingsConfig(openai=settings, xai=settings)
    return config.model_copy(
        update={
            "defaults": config.defaults.model_copy(update={"cli_settings": table}),
            "apps": {
                name: app.model_copy(update={"cli_settings": table})
                for name, app in config.apps.items()
            },
        }
    )


def answer(text="answer"):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def fake_gateway(monkeypatch, handler):
    original = httpx.AsyncClient
    clients = []

    def client(**options):
        assert options["trust_env"] is False
        assert options.get("follow_redirects", False) is False
        created = original(**options, transport=httpx.MockTransport(handler))
        clients.append(created)
        return created

    monkeypatch.setattr(api_runner, "AsyncClient", client)
    return clients
