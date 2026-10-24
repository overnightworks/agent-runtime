"""Buffered API rounds using the run's existing MCP answer and acceptance owners."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Collection, Sequence
from typing import Any

from agent_runtime.errors import raise_without_chain
from agent_runtime.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_runtime.openai_adapter import OpenAIConversation
from agent_runtime.tool_loop import InitialTurn, ToolCall, ToolResult, ToolResultBatch
from agent_runtime.tools import openai_tool_schema
from agent_runtime_client.mcp_wire import (
    LATEST_PROTOCOL_VERSION,
    Call,
    HttpAnswer,
    JsonRpcRequest,
    JsonRpcResponse,
    ListToolsResult,
    McpMethod,
    decide_server_message,
    encode_message,
    read_answer,
    read_payload,
    read_tool_outcome,
)
from agent_runtime_service.capability import Capability
from agent_runtime_service.run_tools import RunTools
from agent_runtime_service.tool_relay import RelayRequest, Unanswered


def require_unprotected(value: object, key: str, capability: Capability | None) -> None:
    """Inspect decoded values through their canonical, Unicode-preserving representation."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if key.encode("utf-8") in encoded or (
        capability is not None and capability.appears_in(encoded)
    ):
        raise_without_chain(ValueError("The API returned protected credentials."))


def _request(message: JsonRpcRequest) -> RelayRequest:
    return RelayRequest(encode_message(message), LATEST_PROTOCOL_VERSION)


def _reply(answer: HttpAnswer | Unanswered) -> JsonRpcResponse:
    if isinstance(answer, HttpAnswer) and 200 <= answer.status < 300:
        reply = read_answer(answer.content_type, answer.body)
        if isinstance(reply, JsonRpcResponse):
            return reply
    raise_without_chain(ValueError("The API tool exchange was refused."))


async def offered_schemas(
    tools: RunTools, key: str, capability: Capability,
) -> list[dict[str, Any]]:
    request = _request(JsonRpcRequest(id=0, method=McpMethod.TOOLS_LIST))
    listing = _reply(await tools.answer(request))
    offered = read_payload(ListToolsResult, listing.result)
    schemas = [openai_tool_schema(tool.name, tool.description, tool.input_schema)
               for tool in offered.tools]
    require_unprotected(schemas, key, capability)
    return schemas


def _prepared_calls(
    calls: Sequence[ToolCall], offered_names: Collection[str],
) -> list[RelayRequest]:
    prepared = [_request(JsonRpcRequest(id=call.tool_use_id, method=McpMethod.TOOLS_CALL,
        params={"name": call.name, "arguments": call.arguments})) for call in calls]
    if not all(isinstance(decide_server_message(request.body, request.protocol_version,
                                               offered_names), Call) for request in prepared):
        raise_without_chain(ValueError("The API requested an invalid tool call."))
    return prepared


async def tool_conversation(
    conversation: OpenAIConversation, initial: InitialTurn, tools: RunTools,
    offered_names: Collection[str], key: str, capability: Capability,
) -> AsyncGenerator[StreamEvent, None]:
    message: InitialTurn | ToolResultBatch = initial
    text = ""
    while True:
        completed = await conversation.complete(message)
        require_unprotected([completed.text, [
            [call.tool_use_id, call.name, call.arguments] for call in completed.calls
        ]], key, capability)
        prospective = text + completed.text
        require_unprotected(prospective, key, capability)
        prepared = _prepared_calls(completed.calls, offered_names)
        text = prospective
        if completed.text:
            yield AssistantTextEvent(text=completed.text)
        if not completed.calls:
            yield FinalEvent(text=text)
            return
        results = []
        for call, request in zip(completed.calls, prepared, strict=True):
            reply = _reply(await tools.answer(request))
            outcome = read_tool_outcome(reply.result)
            results.append(ToolResult(call.tool_use_id, outcome.content, outcome.is_error))
        message = ToolResultBatch(tuple(results))
