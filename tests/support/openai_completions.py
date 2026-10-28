"""Completion payloads and refusal checks the OpenAI-compatible adapter's tests share."""

from __future__ import annotations

import httpx


def completion(*, text="answer", **message):
    return {"choices": [{"message": {"role": "assistant", "content": text, **message},
                         "finish_reason": "stop"}]}


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


def caller_error():
    request = httpx.Request("POST", "http://fake", headers={
        "Authorization": "Bearer synthetic-ambient-secret"})
    return httpx.ReadError("synthetic-ambient-secret", request=request)


def assert_closed_refusal(error):
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "synthetic-ambient-secret" not in str(error)


def tool_completion(calls=None, text=None):
    return {"choices": [{"message": {"role": "assistant", "content": text,
        "tool_calls": calls if calls is not None else [provider_call()]},
        "finish_reason": "tool_calls"}]}


def provider_call(identity="provider-1", name="echo", arguments='{"value": 7}'):
    return {"id": identity, "type": "function", "function": {
        "name": name, "arguments": arguments}}
