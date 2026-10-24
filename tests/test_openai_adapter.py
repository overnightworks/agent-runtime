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


def tool_completion(calls=None, text=None):
    return {"choices": [{"message": {"role": "assistant", "content": text,
        "tool_calls": calls if calls is not None else [provider_call()]},
        "finish_reason": "tool_calls"}]}


def provider_call(identity="provider-1", name="echo", arguments='{"value": 7}'):
    return {"id": identity, "type": "function", "function": {
        "name": name, "arguments": arguments}}


def strict_complete(response):
    from agent_runtime.openai_adapter import OpenAIConversation
    from agent_runtime.tool_loop import InitialTurn

    async def run():
        transport = httpx.MockTransport(lambda request: response)
        async with httpx.AsyncClient(transport=transport) as client:
            conversation = OpenAIConversation(client=client, provider="gateway",
                api_url="http://gateway/v1/chat/completions", api_key="synthetic",
                model="chosen", tool_schemas=[])
            return await conversation.complete(InitialTurn("system", []))
    return asyncio.run(run())


@pytest.mark.parametrize("arguments", [None, {}, "", "[]", "null", '{"x":1,"x":2}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":"\\ud800"}', '[' * 1100])
def test_buffered_round_rejects_malformed_later_arguments_without_partial_return(arguments):
    payload = tool_completion([provider_call(), provider_call("second", arguments=arguments)],
                              text="must stay buffered")
    response = httpx.Response(200, json=payload)
    with pytest.raises(ProviderUnavailableError):
        strict_complete(response)


@pytest.mark.parametrize("case", ["id", "type", "name", "role", "finish", "duplicate", "empty"])
def test_buffered_round_requires_complete_function_calls(case):
    payload = tool_completion()
    message = payload["choices"][0]["message"]
    if case in {"id", "type"}:
        message["tool_calls"][0][case] = "" if case == "id" else "custom"
    elif case == "name":
        message["tool_calls"][0]["function"]["name"] = ""
    elif case == "role":
        message["role"] = "user"
    elif case == "finish":
        payload["choices"][0]["finish_reason"] = "stop"
    elif case == "duplicate":
        message["tool_calls"].append(provider_call())
    else:
        message["tool_calls"] = []
    response = httpx.Response(200, json=payload)
    with pytest.raises(ProviderUnavailableError):
        strict_complete(response)


def test_buffered_history_keeps_only_canonical_calls_and_original_order():
    from agent_runtime.openai_adapter import OpenAIConversation
    from agent_runtime.tool_loop import InitialTurn, ToolResult, ToolResultBatch

    requests = []
    call = provider_call()
    call["private_metadata"] = {"token": "must-not-continue"}
    call["function"]["metadata"] = "must-not-continue"
    responses = iter([tool_completion([call]), completion(text=" done ")])

    def upstream(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            conversation = OpenAIConversation(client=client, provider="gateway",
                api_url="http://gateway/v1/chat/completions", api_key="synthetic",
                model="chosen", tool_schemas=[])
            first = await conversation.complete(InitialTurn("system", []))
            assert [(call.tool_use_id, call.name, call.arguments) for call in first.calls] == [
                ("provider-1", "echo", {"value": 7})]
            final = await conversation.complete(ToolResultBatch((ToolResult(
                "provider-1", "result", False),)))
            assert final.text == " done "
            assert not final.calls
    asyncio.run(run())
    assert "must-not-continue" not in json.dumps(requests)
    assert requests[-1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "provider-1", "content": "result"}


@pytest.mark.parametrize("status,body,reason", [
    (302, b"not json", SafeRouteReasonCode.API_HTTP_ERROR),
    (503, b"not json", SafeRouteReasonCode.API_HTTP_ERROR),
    (200, b'{"choices":[],"choices":[]}', SafeRouteReasonCode.API_PROTOCOL_ERROR),
    (200, b'{"x":NaN}', SafeRouteReasonCode.API_PROTOCOL_ERROR),
    (200, b'[' * 1100, SafeRouteReasonCode.API_PROTOCOL_ERROR),
    (200, b'"\\ud800"', SafeRouteReasonCode.API_PROTOCOL_ERROR),
    (200, b'\xff', SafeRouteReasonCode.API_PROTOCOL_ERROR),
])
def test_buffered_refusal_is_closed_even_under_ambient_exception(status, body, reason):
    from agent_runtime.openai_adapter import OpenAIConversation
    from agent_runtime.tool_loop import InitialTurn

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(status, content=body))
        async with httpx.AsyncClient(transport=transport) as client:
            conversation = OpenAIConversation(client=client, provider="gateway",
                api_url="http://fake/v1/chat/completions", api_key="synthetic",
                model="chosen", tool_schemas=[])
            initial = InitialTurn("system", [])
            try:
                raise caller_error()
            except httpx.ReadError:
                with pytest.raises(ProviderUnavailableError) as raised:
                    await conversation.complete(initial)
            assert raised.value.reason.code is reason
            assert_closed_refusal(raised.value)
    asyncio.run(run())


@pytest.mark.parametrize("payload", list(malformed_completions()))
def test_buffered_terminal_round_retains_complete_text_validation(payload):
    response = httpx.Response(200, json=payload)
    with pytest.raises(ProviderUnavailableError):
        strict_complete(response)


@pytest.mark.parametrize("results", [(), ("wrong",), ("provider-1", "extra"),
                                     ("provider-1", "provider-1")])
def test_buffered_continuation_rejects_an_incomplete_or_mismatched_result_batch(results):
    from agent_runtime.openai_adapter import OpenAIConversation
    from agent_runtime.tool_loop import InitialTurn, ToolResult, ToolResultBatch

    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=tool_completion())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            conversation = OpenAIConversation(client=client, provider="gateway",
                api_url="http://gateway/v1/chat/completions", api_key="synthetic",
                model="chosen", tool_schemas=[])
            await conversation.complete(InitialTurn("system", []))
            batch = ToolResultBatch(tuple(
                ToolResult(identity, "outcome", False) for identity in results))
            with pytest.raises(ProviderUnavailableError):
                await conversation.complete(batch)
    asyncio.run(run())
    assert len(requests) == 1
