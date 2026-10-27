"""Buffered API tools through production admission, acceptance and synthetic peers."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp_app_fake import ScriptedApp, TestAuthority, serving, tool_listing
from service_support import (
    API_GROUP,
    SONGMAKER_GROUP,
    TokenMint,
    decoded_events,
    run_body,
    run_headers,
)
from test_openai_adapter import provider_call, tool_completion
from test_service_api_runner import (
    CONFIGURED_MODEL,
    answer,
    configured,
    fake_gateway,
    with_default_model,
)

from agent_runtime.config import reset_config
from agent_runtime_client.wire import Succeeded
from agent_runtime_service.app import create_app
from agent_runtime_service.cli import installed_runners
from agent_runtime_service.run_liveness import LoopClock


async def post_tools(config, authority, *, records=None, **body):
    mint = TokenMint()
    runners = installed_runners(config, authority.client_context(), LoopClock())
    app = create_app(config, runners=runners,
                     fetch_key_set=mint.fetch_key_set)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://service") as client:
        headers = run_headers(mint.workload(), mint.user(groups=[API_GROUP, SONGMAKER_GROUP]))
        response = await client.post("/runs", json=run_body(tools={"echo": "read_only"}, **body),
                                     headers=headers)
        events = decoded_events(response.content)
        record = await client.get(f"/runs/{events[0].run_id}", headers=headers)
        assert record.status_code == 200
        assert record.json()["state"] == events[-1].type
        if records is not None:
            records.append(record.text)
        return events


def with_origin(config, origin):
    return config.model_copy(update={"apps": {**config.apps,
        "songmaker": config.apps["songmaker"].model_copy(update={"mcp_origin": origin})}})


@pytest.mark.parametrize("count,rounds", [(2, 1), (4, 1), (1, 9)])
@pytest.mark.parametrize("model", [CONFIGURED_MODEL, "chosen/provider"])
def test_real_entry_preserves_order_ids_and_model_across_complete_tool_rounds(
    tmp_path, monkeypatch, count, rounds, model,
):
    authority = TestAuthority(tmp_path)
    requests = []

    def upstream(request):
        assert request.headers["authorization"] == "Bearer synthetic-gateway-key"
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) <= rounds:
            return httpx.Response(200, json=tool_completion([
                provider_call(f"call-{index}") for index in range(count)], text="prefix "))
        return httpx.Response(200, json=answer(" done "))

    clients = fake_gateway(monkeypatch, upstream)
    reset_config()
    with serving(ScriptedApp(), authority.credentials_for("localhost")) as peer:
        config = with_origin(with_default_model(configured(tmp_path), model), peer.origin)
        events = asyncio.run(post_tools(config, authority))
        assert isinstance(events[-1], Succeeded)
        assert events[-1].text == "prefix " * rounds + " done "
        effects = [request for request in peer.requests if request.rpc_method == "tools/call"]
        assert len(effects) == count * rounds
        assert sum(request.rpc_method == "initialize" for request in peer.requests) == 1
        assert sum(request.rpc_method == "tools/list" for request in peer.requests) == 1
    assert len(requests) == rounds + 1
    assert all(set(request) == {"model", "messages", "tools"} for request in requests)
    assert len({request["model"] for request in requests}) == 1
    assert requests[0]["model"] == model
    assert requests[-1]["messages"][-count:] == [
        {"role": "tool", "tool_call_id": f"call-{index}", "content": "echo"}
        for index in range(count)]
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("cookie_value", ["ordinary-session", "synthetic-gateway-key"])
def test_gateway_response_cookies_never_continue_outside_authorization(
    tmp_path, monkeypatch, cookie_value,
):
    authority = TestAuthority(tmp_path)
    requests = []

    def upstream(request):
        requests.append(request)
        payload = tool_completion() if len(requests) <= 2 else answer("done")
        return httpx.Response(200, json=payload,
                              headers={"Set-Cookie": f"gateway_session={cookie_value}; Path=/"})

    clients = fake_gateway(monkeypatch, upstream)
    with serving(ScriptedApp(), authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin), authority))
        assert sum(request.rpc_method == "tools/call" for request in peer.requests) == 2
    assert isinstance(events[-1], Succeeded)
    assert events[-1].text == "done"
    assert len(requests) == 3
    assert all(request.headers["authorization"] == "Bearer synthetic-gateway-key"
               for request in requests)
    assert all("cookie" not in request.headers for request in requests)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("bad", [provider_call("second", arguments="[]"),
                                 provider_call("second", name="not-granted")])
def test_later_invalid_call_refuses_whole_round_before_text_or_effect(tmp_path, monkeypatch, bad):
    authority = TestAuthority(tmp_path)
    fake_gateway(monkeypatch, lambda request: httpx.Response(200, json=tool_completion(
        [provider_call(), bad], text="must not escape")))
    with serving(ScriptedApp(), authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin), authority))
        assert [event.type for event in events] == ["run_started", "failed"]
        assert not any(request.rpc_method == "tools/call" for request in peer.requests)


@pytest.mark.parametrize("names", [["other"], ["echo", "echo"]])
def test_missing_or_duplicate_granted_offer_refuses_before_gateway(tmp_path, monkeypatch, names):
    authority = TestAuthority(tmp_path)
    clients = fake_gateway(monkeypatch, lambda request: pytest.fail("invalid offer sent HTTP"))
    with serving(ScriptedApp(pages=[tool_listing(names)]),
                 authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin), authority))
        assert [event.type for event in events] == ["run_started", "failed"]
    assert all(client.is_closed for client in clients)


def test_checked_offer_alone_supplies_schemas_and_omits_missing_description(tmp_path, monkeypatch):
    authority = TestAuthority(tmp_path)
    requests = []

    def upstream(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=answer())

    fake_gateway(monkeypatch, upstream)
    offered = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}},
                         {"name": "other", "inputSchema": {"type": "object"}}]}
    with serving(ScriptedApp(pages=[offered]), authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin), authority))
    assert isinstance(events[-1], Succeeded)
    assert requests[0]["tools"] == [{"type": "function", "function": {
        "name": "echo", "parameters": {"type": "object"}}}]


@pytest.mark.parametrize("boundary", ["call-count", "call-bytes", "result-bytes",
                                     "call-record", "result-record"])
def test_service_budget_or_recording_refusal_preserves_acceptance_boundary(
    tmp_path, monkeypatch, boundary,
):
    from mcp_app_fake import rpc_result

    from agent_runtime_service.run_records import RunRecorder

    authority = TestAuthority(tmp_path)
    requests = []
    calls = [provider_call("first"), provider_call("second")]
    if boundary == "call-bytes":
        calls[0] = provider_call(arguments=json.dumps({"text": "x" * 4000}))

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=tool_completion(calls))

    original = RunRecorder.accepted

    def record(self, event):
        if event.type == {"call-record": "tool_call", "result-record": "tool_result"}.get(boundary):
            raise RuntimeError("synthetic recording refusal")
        return original(self, event)

    monkeypatch.setattr(RunRecorder, "accepted", record)
    fake_gateway(monkeypatch, upstream)
    content = "x" * 4000 if boundary == "result-bytes" else "accepted"
    responder = ScriptedApp(overrides={"tools/call": lambda request: rpc_result(request,
        {"content": [{"type": "text", "text": content}]})})
    with serving(responder, authority.credentials_for("localhost")) as peer:
        config = with_origin(configured(tmp_path), peer.origin)
        config = config.model_copy(update={"runs": config.runs.model_copy(update={
            "max_tool_calls": 1 if boundary == "call-count" else 30,
            "max_bytes": 3000 if boundary.endswith("bytes") else config.runs.max_bytes})})
        if boundary.endswith("record"):
            run = post_tools(config, authority)
            with pytest.raises(RuntimeError, match="synthetic recording refusal"):
                asyncio.run(run)
            events = []
        else:
            events = asyncio.run(post_tools(config, authority))
        effects = [request for request in peer.requests if request.rpc_method == "tools/call"]
        assert len(effects) == (0 if boundary in {"call-bytes", "call-record"} else 1)
    assert len(requests) == 1
    assert not events or events[-1].type == "failed"


@pytest.mark.parametrize("outcome", ["error", "lost"])
def test_tool_error_and_lost_response_are_results_without_retry(tmp_path, monkeypatch, outcome):
    from mcp_app_fake import DROPPED, rpc_result

    authority = TestAuthority(tmp_path)
    requests = []

    def upstream(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=tool_completion() if len(requests) == 1 else answer())

    fake_gateway(monkeypatch, upstream)
    def effect(request):
        return DROPPED if outcome == "lost" else rpc_result(request, {
            "isError": True, "content": [{"type": "text", "text": "app refused"}]})

    with serving(ScriptedApp(overrides={"tools/call": effect}),
                 authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin), authority))
        assert sum(request.rpc_method == "tools/call" for request in peer.requests) == 1
    assert isinstance(events[-1], Succeeded)
    results = [event for event in events if event.type == "tool_result"]
    assert len(results) == 1
    assert results[0].is_error
    assert requests[1]["messages"][-1]["content"] == results[0].content


@pytest.mark.parametrize("secret_kind", ["gateway", "capability", "signature"])
@pytest.mark.parametrize("location", ["offer", "arguments", "result", "later-text"])
def test_protected_values_never_escape_the_allowed_headers(
    tmp_path, monkeypatch, caplog, secret_kind, location,
):
    from mcp_app_fake import rpc_result
    from service_support import record_signed_tokens

    signed = record_signed_tokens(monkeypatch)
    authority = TestAuthority(tmp_path)
    requests = []
    records = []

    def secret():
        if secret_kind == "gateway":
            return "synthetic-gateway-key"
        return signed[-1] if secret_kind == "capability" else signed[-1].split(".")[-1]

    def upstream(request):
        requests.append(request)
        if len(requests) > 1:
            payload = answer(secret())
        else:
            payload = tool_completion([provider_call(arguments=json.dumps({
                "text": secret() if location == "arguments" else "safe"}))])
        # Escaped ASCII and byte-fragmented delivery exercise decoding before reflection.
        encoded = json.dumps(payload)
        encoded = encoded.replace(secret(), "".join(f"\\u{ord(char):04x}" for char in secret()))
        class Fragments(httpx.AsyncByteStream):
            async def __aiter__(self):
                for byte in encoded.encode():
                    yield bytes([byte])
        return httpx.Response(200, stream=Fragments())

    def listing(request):
        return rpc_result(request, {"tools": [{"name": "echo", "inputSchema": {"type": "object"},
            "description": secret() if location == "offer" else "safe"}]})

    def effect(request):
        return rpc_result(request, {"content": [{"type": "text",
            "text": secret() if location == "result" else "safe"}]})

    clients = fake_gateway(monkeypatch, upstream)
    with serving(ScriptedApp(overrides={"tools/list": listing, "tools/call": effect}),
                 authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin),
                                        authority, records=records))
        assert all(request.headers["authorization"] == f"Bearer {signed[-1]}"
                   for request in peer.requests)
        effect_count = sum(request.rpc_method == "tools/call" for request in peer.requests)
        assert effect_count == (0 if location in {"offer", "arguments"} else 1)
    assert events[-1].type == "failed"
    emitted = json.dumps([event.model_dump(mode="json") for event in events])
    assert secret() not in emitted + caplog.text + "".join(records)
    assert all(secret().encode() not in request.content for request in requests)
    assert len(requests) == {"offer": 0, "arguments": 1, "result": 1, "later-text": 2}[location]
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("secret_kind", ["gateway", "signature"])
@pytest.mark.parametrize("completing_tool_round", [False, True])
def test_protected_value_assembled_across_rounds_refuses_completing_round(
    tmp_path, monkeypatch, secret_kind, completing_tool_round,
):
    from service_support import record_signed_tokens

    signed = record_signed_tokens(monkeypatch)
    authority = TestAuthority(tmp_path)
    requests = []
    fragments = []

    def upstream(request):
        requests.append(json.loads(request.content))
        secret = "synthetic-gateway-key" if secret_kind == "gateway" else signed[-1].split(".")[-1]
        fragments[:] = [secret[:len(secret)//2], secret[len(secret)//2:]]
        if len(requests) == 1:
            payload = tool_completion(text=fragments[0])
        elif completing_tool_round:
            payload = tool_completion([provider_call("later")], text=fragments[1])
        else:
            payload = answer(fragments[1])
        return httpx.Response(200, json=payload)

    fake_gateway(monkeypatch, upstream)
    with serving(ScriptedApp(), authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin), authority))
        assert sum(request.rpc_method == "tools/call" for request in peer.requests) == 1
    assert [event.type for event in events] == [
        "run_started", "assistant_text", "tool_call", "tool_result", "failed"]
    assert events[1].text == fragments[0]
    assert len(requests) == 2


def admitted_tools(origin):
    from dataclasses import replace
    from uuid import uuid4

    from capability_support import canary_token, capability_carrying
    from service_support import alice_admitted_run

    from agent_runtime_client.wire import RunRequest
    from agent_runtime_service.capability import Capability
    from agent_runtime_service.routing import Api

    capability = capability_carrying(canary_token())
    return replace(alice_admitted_run(uuid4()), route=Api("chosen"),
        request=RunRequest.model_validate(run_body(tools={"echo": "read_only"})),
        capability=Capability(
            capability.claims.model_copy(update={"aud": origin}), capability.token))


@pytest.mark.parametrize("stop_at", [None, "call", "result"])
def test_runner_waits_for_each_acceptance_and_closes_without_a_listener(
    tmp_path, monkeypatch, stop_at,
):
    from agent_runtime_service.tool_relay import Relay

    authority = TestAuthority(tmp_path)
    requests = []

    def upstream(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=tool_completion() if len(requests) == 1 else answer())

    clients = fake_gateway(monkeypatch, upstream)
    monkeypatch.setattr(Relay, "start", lambda *args: pytest.fail("API opened a CLI listener"))
    with serving(ScriptedApp(), authority.credentials_for("localhost")) as peer:
        config = with_origin(configured(tmp_path), peer.origin)

        async def run():
            runner = installed_runners(config, authority.client_context(), LoopClock()).api
            events = runner.run(admitted_tools(peer.origin))
            try:
                call = await anext(events)
                assert call.type == "tool_call"
                assert not any(request.rpc_method == "tools/call" for request in peer.requests)
                if stop_at == "call":
                    return
                result = await anext(events)
                assert result.type == "tool_result"
                assert len(requests) == 1
                assert sum(request.rpc_method == "tools/call" for request in peer.requests) == 1
                if stop_at == "result":
                    return
                rest = [event async for event in events]
                assert rest[-1].type == "final"
                assert requests[-1]["messages"][-1] == {
                    "role": "tool", "tool_call_id": "provider-1", "content": "echo"}
            finally:
                await events.aclose()
                assert asyncio.all_tasks() == {asyncio.current_task()}

        asyncio.run(run())
    assert len(requests) == (2 if stop_at is None else 1)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("answer_kind", ["unanswered", "rpc-refusal"])
def test_unanswered_or_rpc_refusal_never_becomes_success(tmp_path, monkeypatch, answer_kind):
    from agent_runtime_client.mcp_wire import (
        JsonRpcErrorCode,
        McpMethod,
        answer_for,
        error_response,
        parse_message,
    )
    from agent_runtime_service.run_tools import RunTools
    from agent_runtime_service.tool_relay import UNANSWERED

    authority = TestAuthority(tmp_path)
    original = RunTools.answer
    requests = []
    async def refused(self, request):
        message = parse_message(request.body)
        if message.method == McpMethod.TOOLS_CALL:
            return UNANSWERED if answer_kind == "unanswered" else answer_for(error_response(
                message.id, JsonRpcErrorCode.INVALID_PARAMS))
        return await original(self, request)

    monkeypatch.setattr(RunTools, "answer", refused)
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=tool_completion())
    fake_gateway(monkeypatch, upstream)
    with serving(ScriptedApp(), authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin), authority))
    assert [event.type for event in events] == ["run_started", "failed"]
    assert len(requests) == 1


@pytest.mark.parametrize("phase", ["discovery", "composition", "prepared-cancel", "effect"])
def test_cancellation_and_composition_failure_close_session_and_http_client(
    tmp_path, monkeypatch, phase,
):
    from mcp_app_fake import HeldRequests

    from agent_runtime_service import api_runner

    authority = TestAuthority(tmp_path)
    held = HeldRequests()
    default = ScriptedApp()
    method = "tools/list" if phase == "discovery" else "tools/call"
    responder = ScriptedApp(overrides={method: held.responder(default)})
    clients = fake_gateway(monkeypatch, lambda request: httpx.Response(200, json=tool_completion()))
    prepared = asyncio.Event()
    if phase == "prepared-cancel":
        original = api_runner.offered_schemas
        async def hold(*args):
            await original(*args)
            prepared.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(api_runner, "offered_schemas", hold)
    if phase == "composition":
        def fail(*args):
            raise RuntimeError("synthetic composition failure")
        monkeypatch.setattr(api_runner, "RelayedEvents", fail)
    with serving(responder, authority.credentials_for("localhost")) as peer:
        config = with_origin(configured(tmp_path), peer.origin)
        async def run():
            runner = installed_runners(config, authority.client_context(), LoopClock()).api
            events = runner.run(admitted_tools(peer.origin))
            step = asyncio.create_task(anext(events))
            try:
                if phase == "composition":
                    with pytest.raises(ValueError):
                        await step
                elif phase == "prepared-cancel":
                    await prepared.wait()
                    step.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await step
                else:
                    if phase == "effect":
                        assert (await step).type == "tool_call"
                        step = asyncio.create_task(anext(events))
                    waiting = (await asyncio.to_thread(held.wait_for, 1))[0]
                    step.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await step
                    await asyncio.to_thread(waiting.wait_for_peer_left)
            finally:
                step.cancel()
                await asyncio.gather(step, return_exceptions=True)
                await events.aclose()
            assert asyncio.all_tasks() == {asyncio.current_task()}
        asyncio.run(run())
    assert all(client.is_closed for client in clients)


async def assert_supervised_tool_ending(app, socket, mint, ending):
    from agent_runtime_client.sse import RunStreamDecoder
    from agent_runtime_client.wire import FailureCode

    events = RunStreamDecoder().feed(socket.body)
    if ending == "idle":
        assert events[-1].code is FailureCode.IDLE_TIMEOUT
    elif ending == "maximum":
        assert events[-1].code is FailureCode.LIMIT_EXCEEDED
    if ending in {"idle", "maximum"}:
        assert sum(event.type in {"succeeded", "failed", "cancelled"}
                   for event in events) == 1
    else:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://service") as client:
            record = await client.get(f"/runs/{socket.run_id}", headers=run_headers(
                mint.workload(), mint.user(groups=[API_GROUP, SONGMAKER_GROUP])))
        assert record.json()["state"] == (
            "cancelled" if ending == "before-call" else "failed")
        if ending == "disconnect":
            assert record.json()["failure_code"] == "interrupted"


def tool_socket(mint):
    from service_support import ClientSocket

    class ToolSocket(ClientSocket):
        first = True
        async def receive(self):
            if self.first:
                self.first = False
                return {"type": "http.request", "body": json.dumps(run_body(
                    tools={"echo": "side_effecting"})).encode(), "more_body": False}
            return await super().receive()

    return ToolSocket(user_token=mint.user(groups=[API_GROUP, SONGMAKER_GROUP]))


async def end_tool_run(socket, clock, limits, ending):
    if ending in {"before-call", "disconnect"}:
        socket.leave()
    else:
        await clock.parked()
        clock.advance_to(limits.heartbeat_interval_s)
        await socket.received(b"event: heartbeat")
        await clock.parked()
        clock.advance_to(limits.idle_timeout_s if ending == "idle"
                         else limits.max_duration_s)


@pytest.mark.parametrize("ending", ["before-call", "disconnect", "idle", "maximum"])
def test_service_supervision_closes_tool_run_at_disconnect_or_deadline(
    tmp_path, monkeypatch, ending,
):
    from mcp_app_fake import HeldRequests
    from service_support import StepClock, SteppedLoop, shared_mint

    authority = TestAuthority(tmp_path)
    clock = StepClock()
    monkeypatch.setattr("agent_runtime_service.app.LoopClock", lambda: clock)
    requests = []
    later_wait = asyncio.Event()
    closed = asyncio.Event()
    held = HeldRequests()
    responder = ScriptedApp(overrides={} if ending == "maximum" else {
        "tools/call": held.responder(ScriptedApp())})

    async def upstream(request):
        requests.append(request)
        if ending == "before-call" or len(requests) > 1:
            later_wait.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return httpx.Response(200, json=tool_completion())

    clients = fake_gateway(monkeypatch, upstream)
    with serving(responder, authority.credentials_for("localhost")) as peer:
        config = with_origin(configured(tmp_path), peer.origin)
        async def run():
            mint = shared_mint()
            socket = tool_socket(mint)
            runners = installed_runners(config, authority.client_context(), clock)
            app = create_app(config, runners=runners,
                             fetch_key_set=mint.fetch_key_set)
            task = asyncio.create_task(app(socket.scope, socket.receive, socket.send))
            waiting = None
            try:
                if ending in {"before-call", "maximum"}:
                    await later_wait.wait()
                else:
                    waiting = (await asyncio.to_thread(held.wait_for, 1))[0]
                await end_tool_run(socket, clock, config.runs, ending)
                await task
                if waiting is not None:
                    await asyncio.to_thread(waiting.wait_for_peer_left)
                await assert_supervised_tool_ending(app, socket, mint, ending)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert asyncio.all_tasks() == {asyncio.current_task()}
        with asyncio.Runner(loop_factory=SteppedLoop) as runner:
            runner.run(run())
        assert sum(request.rpc_method == "tools/call" for request in peer.requests) == (
            0 if ending == "before-call" else 1)
    assert len(requests) == (2 if ending == "maximum" else 1)
    assert all(client.is_closed for client in clients)
    if ending in {"before-call", "maximum"}:
        assert closed.is_set()


@pytest.mark.parametrize("field", ["system", "history"])
@pytest.mark.parametrize("secret_kind", ["gateway", "signature"])
def test_initial_reflection_refuses_before_session_or_gateway_and_clears_ambient_chain(
    tmp_path, monkeypatch, field, secret_kind,
):
    from dataclasses import replace

    from test_openai_adapter import assert_closed_refusal, caller_error

    authority = TestAuthority(tmp_path)
    clients = fake_gateway(monkeypatch, lambda request: pytest.fail("reflected input sent HTTP"))
    with serving(ScriptedApp(), authority.credentials_for("localhost")) as peer:
        admitted = admitted_tools(peer.origin)
        secret = ("synthetic-gateway-key" if secret_kind == "gateway"
                  else admitted.capability.token.split(".")[-1])
        turn = admitted.request.turn.model_dump()
        if field == "system":
            turn["system"] = secret
        else:
            turn["messages"][0]["text"] = secret
        request = admitted.request.model_validate({**admitted.request.model_dump(), "turn": turn})
        admitted = replace(admitted, request=request)
        config = with_origin(configured(tmp_path), peer.origin)
        async def run():
            runner = installed_runners(config, authority.client_context(), LoopClock()).api
            events = runner.run(admitted)
            try:
                try:
                    raise caller_error()
                except httpx.ReadError:
                    with pytest.raises(ValueError) as raised:
                        await anext(events)
                assert_closed_refusal(raised.value)
            finally:
                await events.aclose()
        asyncio.run(run())
        assert not peer.requests
    assert all(client.is_closed for client in clients)


def test_oversized_later_mcp_call_refuses_the_entire_round(tmp_path, monkeypatch):
    from agent_runtime_client.wire import MAX_EVENT_BYTES

    authority = TestAuthority(tmp_path)
    fake_gateway(monkeypatch, lambda request: httpx.Response(200, json=tool_completion([
        provider_call(),
        provider_call("second", arguments=json.dumps({"text": "x" * MAX_EVENT_BYTES}))
    ], text="must stay buffered")))
    with serving(ScriptedApp(), authority.credentials_for("localhost")) as peer:
        events = asyncio.run(post_tools(with_origin(configured(tmp_path), peer.origin), authority))
        assert not any(request.rpc_method == "tools/call" for request in peer.requests)
    assert [event.type for event in events] == ["run_started", "failed"]
