"""The configuration, canned answer and fake gateway the service's API-route tests share."""

from __future__ import annotations

import httpx
from tests.support.api_support import write_api_credentials
from tests.support.service_support import LISTED_MODEL, run_body, service_config

from agent_runtime_service import api_runner
from agent_runtime_service.cli_settings_config import CliSettingsConfig
from agent_runtime_service.config import AgentConfig, DefaultsConfig, Provider, ServiceConfig


def configured(tmp_path, claude_models=(LISTED_MODEL,), **api):
    path = write_api_credentials(tmp_path / "gateway.json", {"shared_key": "synthetic-gateway-key"})
    base = service_config(claude_models=claude_models)
    return ServiceConfig.model_validate({
        **{name: getattr(base, name) for name in ServiceConfig.model_fields},
        "api": {"completion_url": "http://gateway/v1/chat/completions",
                "credential_file": str(path), **api},
    })


CONFIGURED_MODEL = f"{Provider.ANTHROPIC}/{LISTED_MODEL}"


def with_default_model(config, model):
    return config.model_copy(update={"defaults": DefaultsConfig(model=model)})


def with_cli_settings(config, settings):
    """``config`` with the same ``openai`` and ``xai`` tables at ``[defaults]``, every app and the
    agent the API tests run, so a leak from any level reaches the gateway payload."""
    table = CliSettingsConfig(openai=settings, xai=settings)

    def app_with(app):
        agents = {
            name: agent.model_copy(update={"cli_settings": table})
            for name, agent in {run_body()["agent"]: AgentConfig(), **app.agents}.items()
        }
        return app.model_copy(update={"cli_settings": table, "agents": agents})

    return config.model_copy(
        update={
            "defaults": config.defaults.model_copy(update={"cli_settings": table}),
            "apps": {name: app_with(app) for name, app in config.apps.items()},
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
