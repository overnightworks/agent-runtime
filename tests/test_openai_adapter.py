"""The service text entry accepts only complete text; released callers retain their contract."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_runtime.errors import ProviderUnavailableError, SafeRouteReasonCode
from agent_runtime.openai_adapter import complete_openai_compatible_text


def completion(*, text="answer", **message):
    return {"choices": [{"message": {"role": "assistant", "content": text, **message},
                         "finish_reason": "stop"}]}


def complete(response):
    async def run():
        transport = httpx.MockTransport(lambda request: response)
        async with httpx.AsyncClient(transport=transport) as client:
            return await complete_openai_compatible_text(
                client=client, provider="gateway", api_url="http://gateway/v1/chat/completions",
                api_key="synthetic-secret", model="chosen/model", system="system",
                messages=[{"role": "user", "content": "question"}],
            )
    return asyncio.run(run())


@pytest.mark.parametrize("status", [200, 201, 299])
@pytest.mark.parametrize("message", [{}, {"tool_calls": None}, {"tool_calls": []},
                                     {"function_call": None}])
@pytest.mark.parametrize("text", ["answer", " ", "\t\n"])
def test_complete_text_accepts_only_checked_text(status, message, text):
    assert complete(httpx.Response(status, json=completion(text=text, **message))) == text


@pytest.mark.parametrize("status", [100, 301, 302, 307, 400, 401, 402, 403, 429, 500, 503])
@pytest.mark.parametrize("body", [b"not json", json.dumps(completion()).encode()])
def test_http_status_is_checked_before_json(status, body):
    response = httpx.Response(status, content=body)
    with pytest.raises(ProviderUnavailableError) as raised:
        complete(response)
    assert raised.value.reason.code is SafeRouteReasonCode.API_HTTP_ERROR
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def malformed_completions():
    yield from [None, [], {}, {"choices": []}, {"choices": False}, {"choices": [None]},
                {"choices": [{"message": []}]}, {"choices": [{}]}]
    for field, values in {
        "content": ["", None, False, [], {}], "role": [None, "user", False],
        "tool_calls": [False, {}, "", [{"function": {"arguments": "private"}}]],
        "function_call": [False, {}, "", {"name": "tool"}],
    }.items():
        for value in values:
            yield completion(**{field: value}) if field != "content" else completion(text=value)
    for field in ["role", "content"]:
        payload = completion()
        del payload["choices"][0]["message"][field]
        yield payload
    for finish in [None, False, "", "unknown", "length", "content_filter", "tool_calls",
                   "function_call"]:
        payload = completion()
        payload["choices"][0]["finish_reason"] = finish
        yield payload
    payload = completion()
    del payload["choices"][0]["finish_reason"]
    yield payload


@pytest.mark.parametrize("payload", list(malformed_completions()))
def test_incomplete_or_tool_output_is_refused_as_a_whole(payload):
    response = httpx.Response(200, json=payload)
    with pytest.raises(ProviderUnavailableError) as raised:
        complete(response)
    assert raised.value.reason.code is SafeRouteReasonCode.API_PROTOCOL_ERROR
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("status,body", [(200, b"secret invalid json"), (204, b"")])
def test_invalid_json_never_leaves_its_document_in_a_failure_chain(status, body):
    response = httpx.Response(status, content=body)
    with pytest.raises(ProviderUnavailableError) as raised:
        complete(response)
    assert raised.value.reason.code is SafeRouteReasonCode.API_PROTOCOL_ERROR
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def test_released_async_tool_rounds_keep_their_permissive_json_contract(monkeypatch):
    from agent_runtime import openai_adapter
    from agent_runtime.events import FinalEvent, ToolResultEvent
    from agent_runtime.tool_loop import ToolOutcome

    original = httpx.AsyncClient
    requests = []
    replies = iter([
        {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "read-1", "function": {"name": "read", "arguments": '{"id": 7}'}}
        ]}}]},
        {"choices": [{"message": {"content": "done"}}]},
    ])

    def upstream(request):
        requests.append(json.loads(request.content))
        return httpx.Response(302, json=next(replies))

    monkeypatch.setattr(openai_adapter.httpx, "AsyncClient", lambda **options: original(
        **options, transport=httpx.MockTransport(upstream)))

    def executor(name, arguments):
        assert (name, arguments) == ("read", {"id": 7})
        return ToolOutcome("read result", False)

    async def run():
        return [event async for event in openai_adapter.stream_openai_compatible_turn(
            provider="gateway", api_url="http://gateway/v1/chat/completions",
            api_key="synthetic", model="model", system="system", messages=[],
            tool_schemas=[], executor=executor)]

    events = asyncio.run(run())
    assert isinstance(events[-1], FinalEvent)
    assert events[-1].text == "done"
    assert any(isinstance(event, ToolResultEvent) and event.content == "read result"
               for event in events)
    assert requests[-1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "read-1", "content": "read result"}


def test_released_sync_call_keeps_its_json_and_timeout_contract(monkeypatch):
    from agent_runtime.openai_adapter import call_openai_compatible_once

    def post(url, **options):
        assert url == "http://gateway/v1/chat/completions"
        assert options["timeout"] == 17
        assert options["json"] == {"model": "model", "messages": [
            {"role": "system", "content": "system"}, {"role": "user", "content": "question"}]}
        return httpx.Response(302, json={"choices": [{"message": {"content": "done"}}]})

    monkeypatch.setattr(httpx, "post", post)
    assert call_openai_compatible_once(
        provider="gateway", api_url="http://gateway/v1/chat/completions", api_key="synthetic",
        model="model", prompt="question", system="system", timeout=17) == "done"


def caller_error():
    request = httpx.Request("POST", "http://fake", headers={
        "Authorization": "Bearer synthetic-ambient-secret"})
    return httpx.ReadError("synthetic-ambient-secret", request=request)


def assert_closed_refusal(error):
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "synthetic-ambient-secret" not in str(error)


@pytest.mark.parametrize("status,body,reason", [
    (503, b"unavailable", SafeRouteReasonCode.API_HTTP_ERROR),
    (200, b"invalid json", SafeRouteReasonCode.API_PROTOCOL_ERROR),
])
def test_text_refusal_removes_active_caller_chain(status, body, reason):
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(status, content=body))
        async with httpx.AsyncClient(transport=transport) as client:
            try:
                raise caller_error()
            except httpx.ReadError:
                with pytest.raises(ProviderUnavailableError) as raised:
                    await complete_openai_compatible_text(
                        client=client, provider="gateway", api_url="http://fake/v1/chat/completions",
                        api_key="synthetic", model="model", system="system", messages=[])
            assert_closed_refusal(raised.value)
            assert raised.value.reason.code is reason
    asyncio.run(run())


def test_empty_completed_text_is_a_protocol_refusal():
    response = httpx.Response(201, json=completion(text=""))
    with pytest.raises(ProviderUnavailableError) as raised:
        complete(response)
    assert raised.value.reason.code is SafeRouteReasonCode.API_PROTOCOL_ERROR
