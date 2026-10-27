"""The real service's buffered API route, with synthetic credentials and fake HTTP only."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from service_support import (
    API_GROUP,
    LISTED_MODEL,
    SONGMAKER_GROUP,
    TokenMint,
    decoded_events,
    in_loop,
    run_body,
    run_headers,
    service_config,
)
from service_support import logging_state_snapshot as logging_state_snapshot
from test_openai_adapter import assert_closed_refusal, caller_error, malformed_completions

from agent_runtime.config import reset_config
from agent_runtime_client.wire import AssistantText, Failed, FailureCode, Succeeded
from agent_runtime_service import api_runner
from agent_runtime_service.app import create_app
from agent_runtime_service.cli import installed_runners
from agent_runtime_service.config import AgentConfig, DefaultsConfig, Provider, ServiceConfig
from agent_runtime_service.run_liveness import LoopClock


def credentials(path: Path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


def configured(tmp_path, **api):
    path = credentials(tmp_path / "gateway.json", {"shared_key": "synthetic-gateway-key"})
    base = service_config()
    return ServiceConfig.model_validate({
        **{name: getattr(base, name) for name in ServiceConfig.model_fields},
        "api": {"completion_url": "http://gateway/v1/chat/completions",
                "credential_file": str(path), **api},
    })


PAID_AGENT = "paid-agent"
CONFIGURED_MODEL = f"{Provider.CLAUDE}/{LISTED_MODEL}"


def with_default_model(config, model):
    return config.model_copy(update={"defaults": DefaultsConfig(model=model)})


def answer(text="answer"):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


async def service_response(config, **body):
    mint = TokenMint()
    app = create_app(config, runners=installed_runners(config, None, LoopClock()),
                     fetch_key_set=mint.fetch_key_set)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://service") as client:
        return await client.post("/runs", json=run_body(**body), headers=run_headers(
            mint.workload(), mint.user(groups=[API_GROUP, SONGMAKER_GROUP])))


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


@pytest.mark.parametrize("model", [CONFIGURED_MODEL, "grok/synthetic"])
def test_api_only_service_passes_exact_model_and_no_identity(tmp_path, monkeypatch, model):
    config = with_default_model(configured(tmp_path), model)
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=answer())

    clients = fake_gateway(monkeypatch, upstream)
    reset_config()
    response = asyncio.run(service_response(config))
    events = decoded_events(response.content)
    assert [event.type for event in events] == ["run_started", "assistant_text", "succeeded"]
    assert events[-1].text == "answer"
    assert json.loads(requests[0].content) == {
        "model": model,
        "messages": [{"role": "system", "content": "You help write songs."},
                     {"role": "user", "content": "Hi"}], "tools": [],
    }
    assert requests[0].headers["authorization"] == "Bearer synthetic-gateway-key"
    assert clients[0].is_closed


@pytest.mark.parametrize("status", [302, 401, 402, 429, 500])
def test_http_refusal_has_no_answer_or_success(tmp_path, monkeypatch, status):
    config = configured(tmp_path)
    clients = fake_gateway(monkeypatch, lambda request: httpx.Response(status, json=answer()))
    response = asyncio.run(service_response(config))
    events = decoded_events(response.content)
    assert not any(isinstance(event, AssistantText | Succeeded) for event in events)
    assert isinstance(events[-1], Failed)
    assert events[-1].code is FailureCode.PROVIDER_UNAVAILABLE
    assert clients[0].is_closed


@pytest.mark.parametrize("url", ["https://gateway/v1/chat/completions", "ftp://gateway/v1/chat/completions",
    "http://user:secret@gateway/v1/chat/completions", "http://gateway/v1/chat/completions?x=1",
    "http://gateway:0/v1/chat/completions", "http://gateway:65536/v1/chat/completions",
    "http://gateway/v1/chat/completions#fragment", "http://gateway/v1/chat/%63ompletions",
    "http://gateway/other", "http://gateway/V1/chat/completions",
    "http://gäteway/v1/chat/completions", "http://gateway\n/v1/chat/completions"])
def test_invalid_api_url_and_missing_tls_trust_are_refused(tmp_path, url):
    with pytest.raises(ValidationError):
        configured(tmp_path, completion_url=url)


@pytest.mark.parametrize("payload", list(malformed_completions()))
def test_protocol_refusal_never_emits_answer_or_success(tmp_path, monkeypatch, payload):
    config = configured(tmp_path)
    clients = fake_gateway(monkeypatch, lambda request: httpx.Response(200, json=payload))
    events = decoded_events(asyncio.run(service_response(config)).content)
    assert [event.type for event in events] == ["run_started", "failed"]
    assert events[-1].code is FailureCode.PROVIDER_ERROR
    assert clients[0].is_closed


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout])
def test_transport_errors_are_unavailable_and_do_not_leak(
    tmp_path, monkeypatch, caplog, error_type,
):
    config = configured(tmp_path)

    def upstream(request):
        raise error_type("synthetic-gateway-key", request=request)

    clients = fake_gateway(monkeypatch, upstream)
    response = asyncio.run(service_response(config))
    events = decoded_events(response.content)
    assert [event.type for event in events] == ["run_started", "failed"]
    assert events[-1].code is FailureCode.PROVIDER_UNAVAILABLE
    assert "synthetic-gateway-key" not in response.text + caplog.text
    assert clients[0].is_closed


@pytest.mark.parametrize("status,body", [(204, b""), (200, b"synthetic-gateway-key"),
                                         (403, b"synthetic-gateway-key")])
def test_body_errors_are_classified_after_status_without_exposing_body(
    tmp_path, monkeypatch, caplog, status, body,
):
    config = configured(tmp_path)
    fake_gateway(monkeypatch, lambda request: httpx.Response(status, content=body))
    response = asyncio.run(service_response(config))
    events = decoded_events(response.content)
    assert [event.type for event in events] == ["run_started", "failed"]
    assert events[-1].code is (FailureCode.PROVIDER_UNAVAILABLE if status == 403
                              else FailureCode.PROVIDER_ERROR)
    assert "synthetic-gateway-key" not in response.text + caplog.text


@pytest.mark.parametrize("document", [None, [], {}, {"shared_key": None}, {"shared_key": False},
    {"shared_key": ""}, {"shared_key": " token"}, {"shared_key": "bad\nkey"},
    {"shared_key": "key=middle"}, {"unknown": "key"}, {"app_keys": []},
    {"app_keys": {"unknown": "key"}}, {"app_keys": {"songmaker": {"nested": "key"}}},
    {"shared_key": "good", "app_keys": {"songmaker": ""}}])
def test_bad_credentials_never_fall_back_or_expose_document(tmp_path, document):
    from agent_runtime_service.api_credentials import InvalidApiCredentials, read_api_credentials

    path = credentials(tmp_path / "credentials.json", document)
    registered_apps = frozenset({"songmaker"})
    with pytest.raises(InvalidApiCredentials) as raised:
        read_api_credentials(path, registered_apps)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("contents", [b'{"shared_key":"secret","shared_key":"other"}',
    b'{"app_keys":{"songmaker":"secret","songmaker":"other"}}', b'\xff',
    b'{"shared_key":"secret",', b'[' * 1100])
def test_invalid_json_or_duplicate_credentials_have_no_parser_chain(tmp_path, contents):
    from agent_runtime_service.api_credentials import InvalidApiCredentials, read_api_credentials

    path = tmp_path / "credentials.json"
    path.write_bytes(contents)
    path.chmod(0o600)
    registered_apps = frozenset({"songmaker"})
    with pytest.raises(InvalidApiCredentials) as raised:
        read_api_credentials(path, registered_apps)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def test_credentials_are_hidden_and_selected_by_app(tmp_path):
    from agent_runtime_service.api_credentials import read_api_credentials

    path = credentials(tmp_path / "credentials.json", {
        "shared_key": "synthetic-shared", "app_keys": {"songmaker": "synthetic-app"}})
    loaded = read_api_credentials(path, frozenset({"songmaker", "presentator"}))
    assert loaded.for_app("songmaker").get_secret_value() == "synthetic-app"
    assert loaded.for_app("presentator").get_secret_value() == "synthetic-shared"
    assert "synthetic" not in repr(loaded)


def test_rotation_to_per_app_only_keys_is_a_file_only_change(tmp_path, monkeypatch):
    from service_support import PRESENTATOR_CLIENT, SONGMAKER_CLIENT

    config = configured(tmp_path)
    sent = []

    def upstream(request):
        sent.append(request.headers["authorization"])
        return httpx.Response(200, json=answer())

    clients = fake_gateway(monkeypatch, upstream)

    async def run():
        mint = TokenMint()
        app = create_app(config, runners=installed_runners(config, None, LoopClock()),
                         fetch_key_set=mint.fetch_key_set)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://service") as client:
            for document, name, workload in [
                ({"shared_key": "first"}, "songmaker", SONGMAKER_CLIENT),
                ({"shared_key": "shared", "app_keys": {"songmaker": "preferred"}},
                 "songmaker", SONGMAKER_CLIENT),
                ({"app_keys": {"songmaker": "synthetic-song-key",
                               "presentator": "synthetic-slides-key"}},
                 "songmaker", SONGMAKER_CLIENT),
                ({"app_keys": {"songmaker": "synthetic-song-key",
                               "presentator": "synthetic-slides-key"}},
                 "presentator", PRESENTATOR_CLIENT),
            ]:
                credentials(config.api.credential_file, document)
                response = await client.post("/runs", json=run_body(), headers=run_headers(
                    mint.workload(workload), mint.user(groups=[API_GROUP, f"/apps/{name}"],
                                                     azp=workload)))
                assert isinstance(decoded_events(response.content)[-1], Succeeded)

    asyncio.run(run())
    assert sent == ["Bearer first", "Bearer preferred",
                    "Bearer synthetic-song-key", "Bearer synthetic-slides-key"]
    assert len(clients) == 4
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("change", ["missing", "public", "malformed", "no-app", "invalid-app"])
def test_invalid_rotated_credentials_refuse_before_http(tmp_path, monkeypatch, change):
    config = configured(tmp_path)
    clients = fake_gateway(monkeypatch, lambda request: pytest.fail("credential refusal sent HTTP"))
    if change == "missing":
        config.api.credential_file.unlink()
    elif change == "public":
        config.api.credential_file.chmod(0o644)
    elif change == "malformed":
        config.api.credential_file.write_bytes(b"invalid secret")
    else:
        credentials(config.api.credential_file, {"app_keys": {"presentator": "key"}} if
                    change == "no-app" else {"shared_key": "good", "app_keys": {"songmaker": ""}})
    events = decoded_events(asyncio.run(service_response(config)).content)
    assert events[-1].code is FailureCode.PROVIDER_UNAVAILABLE
    assert not clients


@pytest.mark.parametrize("reflected", ["gateway", "capability"])
def test_fragmented_reflection_reaches_no_frame_record_or_log(
    tmp_path, monkeypatch, caplog, reflected,
):
    from service_support import record_signed_tokens

    config = configured(tmp_path)
    signed = record_signed_tokens(monkeypatch)

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self):
            secret = "synthetic-gateway-key" if reflected == "gateway" else signed[-1]
            content = json.dumps(answer(f"prefix {secret} suffix")).encode()
            for byte in content:
                yield bytes([byte])

    fake_gateway(monkeypatch, lambda request: httpx.Response(200, stream=Fragments()))

    async def run():
        mint = TokenMint()
        app = create_app(config, runners=installed_runners(config, None, LoopClock()),
                         fetch_key_set=mint.fetch_key_set)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://service") as client:
            headers = run_headers(mint.workload(), mint.user(groups=[API_GROUP, SONGMAKER_GROUP]))
            response = await client.post("/runs", json=run_body(), headers=headers)
            events = decoded_events(response.content)
            assert [event.type for event in events] == ["run_started", "failed"]
            assert events[-1].code is FailureCode.PROVIDER_ERROR
            record = await client.get(f"/runs/{events[0].run_id}", headers=headers)
            assert record.status_code == 200
            assert "synthetic-gateway-key" not in response.text + record.text + caplog.text
            assert signed[-1] not in response.text + record.text + caplog.text

    asyncio.run(run())


def test_repeated_submissions_each_send_one_http_request(tmp_path, monkeypatch):
    config = configured(tmp_path)
    sent = []

    def upstream(request):
        sent.append(request)
        return httpx.Response(200, json=answer())

    clients = fake_gateway(monkeypatch, upstream)
    first = asyncio.run(service_response(config))
    second = asyncio.run(service_response(config))
    assert len(sent) == len(clients) == 2
    assert decoded_events(first.content)[0].run_id != decoded_events(second.content)[0].run_id


def test_the_api_runner_is_installed_beside_the_cli_runner(tmp_path):
    runners = installed_runners(configured(tmp_path), None, LoopClock())
    assert runners.cli is not None
    assert runners.api is not None
    assert installed_runners(service_config(), None, LoopClock()).api is None


@pytest.mark.parametrize("ending", ["answer", "idle", "maximum", "disconnect"])
@in_loop
async def test_buffered_http_keeps_heartbeats_and_closes_on_every_ending(
    tmp_path, monkeypatch, ending,
):
    from service_support import ClientSocket, StepClock, shared_mint

    from agent_runtime_client.sse import RunStreamDecoder

    config = configured(tmp_path)
    clock = StepClock()
    monkeypatch.setattr("agent_runtime_service.app.LoopClock", lambda: clock)
    release = asyncio.Event()
    closed = asyncio.Event()

    async def upstream(request):
        try:
            await release.wait()
            return httpx.Response(200, json=answer())
        finally:
            closed.set()

    clients = fake_gateway(monkeypatch, upstream)
    mint = shared_mint()
    socket = ClientSocket(user_token=mint.user(groups=[API_GROUP, SONGMAKER_GROUP]))
    app = create_app(config, runners=installed_runners(config, None, clock),
                     fetch_key_set=mint.fetch_key_set)
    response = asyncio.create_task(app(socket.scope, socket.receive, socket.send))
    try:
        await socket.received(b"event: run_started")
        assert await clock.parked() == config.runs.heartbeat_interval_s
        clock.advance_to(config.runs.heartbeat_interval_s)
        await clock.parked()
        assert [event.type for event in RunStreamDecoder().feed(socket.body)] == [
            "run_started", "heartbeat"]
        if ending == "answer":
            release.set()
        elif ending == "disconnect":
            socket.leave()
        else:
            clock.advance_to(config.runs.idle_timeout_s if ending == "idle"
                             else config.runs.max_duration_s)
        await response
    finally:
        response.cancel()
        await asyncio.gather(response, return_exceptions=True)
    assert closed.is_set()
    assert clients[0].is_closed
    events = RunStreamDecoder().feed(socket.body)
    if ending == "answer":
        assert isinstance(events[-1], Succeeded)
    elif ending != "disconnect":
        assert events[-1].code is (FailureCode.IDLE_TIMEOUT if ending == "idle"
                                  else FailureCode.LIMIT_EXCEEDED)
    assert asyncio.all_tasks() == {asyncio.current_task()}


@pytest.mark.parametrize("backend", [False, True])
def test_backend_absence_and_disabled_entitlement_are_distinct(tmp_path, monkeypatch, backend):
    config = configured(tmp_path)
    fields = {name: getattr(config, name) for name in ServiceConfig.model_fields}
    fields["api"] = None
    if not backend:
        fields["identity"] = config.identity.model_copy(update={"api_groups": frozenset()})
        fields["route"] = None
    disabled = ServiceConfig.model_validate(fields)
    response = asyncio.run(service_response(disabled))
    if backend:
        assert decoded_events(response.content)[-1].code is FailureCode.PROVIDER_UNAVAILABLE
    else:
        assert response.status_code == 403
        assert response.json()["code"] == "not_enabled"


def test_api_requires_routing_and_valid_startup_credentials(tmp_path):
    config = configured(tmp_path)
    fields = {name: getattr(config, name) for name in ServiceConfig.model_fields}
    fields["identity"] = config.identity.model_copy(update={"api_groups": frozenset()})
    fields["route"] = None
    with pytest.raises(ValidationError, match="requires identity.api_groups"):
        ServiceConfig.model_validate(fields)
    fields = {name: getattr(config, name) for name in ServiceConfig.model_fields}
    config.api.credential_file.write_bytes(b"invalid-synthetic-key-document")
    with pytest.raises(ValidationError, match="API credential file is missing or invalid"):
        ServiceConfig.model_validate(fields)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/v1/chat/completions",
    "http://[::1]:8080/v1/chat/completions", "http://gateway.internal/v1/chat/completions"])
def test_config_accepts_the_explicit_local_gateway_hosts(tmp_path, url):
    assert configured(tmp_path, completion_url=url).api.completion_url == url


@pytest.mark.parametrize("api", [
    {"ca_file": "/tmp/unused.pem"}, {"credential_file": "relative.json"},
    {"completion_url": "http://[::1]suffix/v1/chat/completions"},
    {"completion_url": "http://bad..host/v1/chat/completions"},
    {"completion_url": "http://gateway:/v1/chat/completions"},
    {"completion_url": "https://gateway/v1/chat/completions", "ca_file": "relative.pem"},
])
def test_config_refuses_partial_or_contradictory_gateway_settings(tmp_path, api):
    with pytest.raises(ValidationError):
        configured(tmp_path, **api)


def test_https_uses_only_the_explicit_ca_and_hostname_verification(tmp_path, monkeypatch):
    import ssl

    from mcp_app_fake import TestAuthority

    authority = TestAuthority(tmp_path)
    config = configured(tmp_path, completion_url="https://gateway/v1/chat/completions",
                        ca_file=str(authority.ca_file))
    original = api_runner.AsyncClient
    verified = []

    def client(**options):
        context = options["verify"]
        assert isinstance(context, ssl.SSLContext)
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.get_ca_certs(binary_form=True) == authority.client_context().get_ca_certs(
            binary_form=True)
        verified.append(context)
        return original(**options, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=answer())))

    monkeypatch.setenv("SSL_CERT_FILE", "/does-not-exist")
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-be-used.invalid")
    monkeypatch.setattr(api_runner, "AsyncClient", client)
    assert isinstance(decoded_events(asyncio.run(service_response(config)).content)[-1], Succeeded)
    assert len(verified) == 1


def test_unreadable_ca_stops_runner_installation(tmp_path):
    config = configured(tmp_path, completion_url="https://gateway/v1/chat/completions",
                        ca_file=str(tmp_path / "missing.pem"))
    clock = LoopClock()
    with pytest.raises(OSError):
        installed_runners(config, None, clock)


def test_removed_model_is_never_substituted_retried_or_routed_to_cli(tmp_path, monkeypatch):
    config = with_default_model(configured(tmp_path), "grok/removed")
    sent = []

    def upstream(request):
        sent.append(json.loads(request.content)["model"])
        return httpx.Response(404, content=b"model removed")

    fake_gateway(monkeypatch, upstream)
    events = decoded_events(asyncio.run(service_response(config)).content)
    assert sent == ["grok/removed"]
    assert [event.type for event in events] == ["run_started", "failed"]
    assert events[-1].code is FailureCode.PROVIDER_UNAVAILABLE


def test_full_cli_capacity_does_not_block_the_real_api_runner(tmp_path, monkeypatch):
    from service_support import SUBSCRIPTION_GROUP, ClientSocket, GatedRunner, shared_mint

    from agent_runtime_service.runners import Runners

    config = configured(tmp_path)
    paid = config.apps["songmaker"].model_copy(
        update={"agents": {PAID_AGENT: AgentConfig(model="grok/synthetic")}})
    config = config.model_copy(update={
        "runs": config.runs.model_copy(update={"max_parallel_cli_runs": 1}),
        "apps": {**config.apps, "songmaker": paid}})
    fake_gateway(monkeypatch, lambda request: httpx.Response(200, json=answer()))

    async def run():
        mint = shared_mint()
        cli = GatedRunner()
        app = create_app(config, runners=Runners(
            cli=cli, api=installed_runners(config, None, LoopClock()).api),
            fetch_key_set=mint.fetch_key_set)
        socket = ClientSocket()
        subscription = asyncio.create_task(app(socket.scope, socket.receive, socket.send))
        try:
            await socket.received(b"event: run_started")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://service") as client:
                both = run_headers(mint.workload(), mint.user(groups=[
                    API_GROUP, SUBSCRIPTION_GROUP, SONGMAKER_GROUP]))
                busy = await client.post(
                    "/runs", json=run_body(), headers=both)
                assert busy.status_code == 429
                paid = await client.post(
                    "/runs", json=run_body(purpose=PAID_AGENT), headers=both)
                assert isinstance(decoded_events(paid.content)[-1], Succeeded)
        finally:
            socket.leave()
            await subscription
        assert cli.closes == 1

    asyncio.run(run())


def test_process_entry_installs_the_api_route_beside_the_probed_cli_program(
    tmp_path, monkeypatch, logging_state_restored,
):
    import uvicorn
    from mcp_app_fake import TestAuthority

    from agent_runtime_service import cli
    from agent_runtime_service import config as configuration
    from agent_runtime_service.config import McpConfig

    config = configured(tmp_path)
    config = config.model_copy(update={"mcp": McpConfig(
        ca_file=str(TestAuthority(tmp_path).ca_file))})
    fake_gateway(monkeypatch, lambda request: httpx.Response(200, json=answer()))
    monkeypatch.setattr(cli, "harden_process", lambda: None)
    monkeypatch.setattr(configuration, "load_config", lambda path: config)
    mint = TokenMint()
    original = create_app
    monkeypatch.setattr("agent_runtime_service.app.create_app", lambda config, **options: original(
        config, **options, fetch_key_set=mint.fetch_key_set))
    served = []

    def serve(app, **options):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://service") as client:
                return await client.post("/runs", json=run_body(), headers=run_headers(
                    mint.workload(), mint.user(groups=[API_GROUP, SONGMAKER_GROUP])))
        served.extend(decoded_events(asyncio.run(run()).content))

    monkeypatch.setattr(uvicorn, "run", serve)
    assert cli.main(["--config", str(tmp_path / "service.toml")]) == 0
    assert isinstance(served[-1], Succeeded)


@pytest.mark.parametrize("text", ["", " ", "\t\n"])
def test_completed_text_preserves_the_wire_nonempty_boundary(tmp_path, monkeypatch, text):
    config = configured(tmp_path)
    clients = fake_gateway(monkeypatch, lambda request: httpx.Response(201, json=answer(text)))
    events = decoded_events(asyncio.run(service_response(config)).content)
    if text:
        assert [event.type for event in events] == ["run_started", "assistant_text", "succeeded"]
        assert events[1].text == events[-1].text == text
    else:
        assert [event.type for event in events] == ["run_started", "failed"]
        assert events[-1].code is FailureCode.PROVIDER_ERROR
    assert clients[0].is_closed


@pytest.mark.parametrize("boundary", ["reader", "selection"])
def test_credential_refusal_removes_active_caller_chain(tmp_path, boundary):
    from agent_runtime_service.api_credentials import InvalidApiCredentials, read_api_credentials

    path = credentials(tmp_path / "keys.json", {"app_keys": {"other": "synthetic"}})
    registered_apps = frozenset({"app", "other"})
    loaded = read_api_credentials(path, registered_apps)
    if boundary == "reader":
        path.write_text("invalid document")
    try:
        raise caller_error()
    except httpx.ReadError:
        if boundary == "reader":
            with pytest.raises(InvalidApiCredentials) as raised:
                read_api_credentials(path, registered_apps)
        else:
            with pytest.raises(InvalidApiCredentials) as raised:
                loaded.for_app("app")
    assert_closed_refusal(raised.value)


@pytest.mark.parametrize("scenario", ["credentials", "http", "protocol", "reflection"])
@in_loop
async def test_runner_refusal_removes_active_caller_chain(tmp_path, monkeypatch, scenario):
    from dataclasses import replace
    from uuid import uuid4

    from service_support import alice_admitted_run

    from agent_runtime_client.wire import RunRequest
    from agent_runtime_service.routing import Api
    from agent_runtime_service.runners import ApiUnavailable

    config = configured(tmp_path)
    body = run_body()
    admitted = replace(alice_admitted_run(uuid4()), request=RunRequest.model_validate(body),
                       model="claude/synthetic", route=Api())
    if scenario == "credentials":
        config.api.credential_file.unlink()

    def upstream(request):
        assert scenario != "credentials"
        if scenario == "http":
            return httpx.Response(503, content=b"unavailable")
        if scenario == "protocol":
            return httpx.Response(200, content=b"not-json")
        return httpx.Response(200, json=answer("synthetic-gateway-key"))

    clients = fake_gateway(monkeypatch, upstream)
    events = api_runner.ApiRunner(config, tls_context=None, clock=LoopClock()).run(admitted)
    try:
        expected = ApiUnavailable if scenario in {"credentials", "http"} else ValueError
        try:
            raise caller_error()
        except httpx.ReadError:
            with pytest.raises(expected) as raised:
                await anext(events)
        assert_closed_refusal(raised.value)
    finally:
        await events.aclose()
    assert all(client.is_closed for client in clients)


def test_api_ca_startup_refusal_names_its_own_configuration(tmp_path, monkeypatch, capsys):
    from mcp_app_fake import TestAuthority

    from agent_runtime_service import cli
    from agent_runtime_service import config as configuration
    from agent_runtime_service.config import McpConfig

    config = configured(tmp_path, completion_url="https://gateway/v1/chat/completions",
                        ca_file=str(tmp_path / "absent-api.pem"))
    config = config.model_copy(update={"mcp": McpConfig(
        ca_file=str(TestAuthority(tmp_path).ca_file))})
    monkeypatch.setattr(cli, "harden_process", lambda: None)
    monkeypatch.setattr(configuration, "load_config", lambda path: config)
    assert cli.main(["--config", str(tmp_path / "service.toml")]) == cli.EXIT_BAD_CONFIG
    assert "api.ca_file: cannot be loaded" in capsys.readouterr().err
