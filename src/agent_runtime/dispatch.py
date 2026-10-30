"""Run one turn on the provider and route the host selected — never another.

A route is chosen by the host and executed here, with no fallback: a failing
CLI never silently becomes an HTTP call, because a user who signed a CLI
in did not agree to spend an API key. Everything a turn needs beyond the
route — the tools it may call and who may run them — arrives from the host
as a catalog and an executor.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from agent_runtime.catalog import ProviderRoute
from agent_runtime.claude.adapter import (
    call_claude_once,
    stream_claude_api_turn,
    stream_claude_turn,
)
from agent_runtime.claude.provider import (
    UnavailableError as ClaudeUnavailableError,
)
from agent_runtime.codex.transport import CodexCliToolTransport
from agent_runtime.config import HostWiringError, current_config
from agent_runtime.constants import (
    GROK_CHAT_URL,
    OPENAI_CHAT_URL,
    ProviderName,
)
from agent_runtime.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
    ToolProtocolRejectionCode,
    normalize_route_failure,
    refuse_empty_model,
)
from agent_runtime.events import StreamEvent
from agent_runtime.grok.transport import GrokCliToolTransport
from agent_runtime.openai_adapter import (
    call_openai_compatible_once,
    stream_openai_compatible_turn,
)
from agent_runtime.tool_loop import (
    ToolExecutor,
    ToolLoopLimitError,
    ToolLoopProtocolError,
    ToolTransport,
    stream_tool_loop,
)
from agent_runtime.tools import (
    ToolCatalog,
    anthropic_tool_schemas,
    openai_tool_schemas,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ApiConnection:
    api_key: str
    api_url: str | None = None


async def stream_turn(
    *,
    provider: str,
    route: ProviderRoute,
    model: str | None,
    user_id: str,
    system: str,
    messages: list[dict[str, str]],
    executor: ToolExecutor,
    catalog: ToolCatalog,
    correlation_id: str | None = None,
) -> AsyncIterator[StreamEvent]:
    if provider not in ProviderName:
        raise _unavailable(provider, route, SafeRouteReasonCode.ROUTE_FAILED)
    refuse_empty_model(provider, route.value, model)
    if model is None and route is not ProviderRoute.CLI:
        raise _unavailable(provider, route, SafeRouteReasonCode.ROUTE_FAILED)
    try:
        stream = _stream_for_route(
            provider=provider,
            route=route,
            model=model,
            user_id=user_id,
            system=system,
            messages=messages,
            executor=executor,
            catalog=catalog,
            correlation_id=correlation_id,
        )
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()
    except HostWiringError:
        raise
    except ProviderUnavailableError as exc:
        if exc.reason is not None:
            raise
        raise _unavailable(provider, route, SafeRouteReasonCode.ROUTE_FAILED) from exc
    except Exception as exc:
        log.warning(
            "Turn failed provider=%s route=%s class=%s",
            provider,
            route,
            type(exc).__name__,
        )
        raise _unavailable(provider, route, SafeRouteReasonCode.ROUTE_FAILED) from exc


def _stream_for_route(
    *,
    provider: str,
    route: ProviderRoute,
    model: str | None,
    user_id: str,
    system: str,
    messages: list[dict[str, str]],
    executor: ToolExecutor,
    catalog: ToolCatalog,
    correlation_id: str | None,
) -> AsyncIterator[StreamEvent]:
    if route is ProviderRoute.CLI:
        if provider == "claude":
            return stream_claude_turn(
                user_id=user_id,
                system=system,
                model=model,
                messages=messages,
                correlation_id=correlation_id,
            )
        if provider == "grok":
            return _stream_grok_cli_tool_turn(
                model=model,
                system=system,
                messages=messages,
                executor=executor,
                catalog=catalog,
                correlation_id=correlation_id,
            )
        if provider == "codex":
            return _stream_codex_cli_tool_turn(
                model=model,
                system=system,
                messages=messages,
                executor=executor,
                catalog=catalog,
                correlation_id=correlation_id,
            )
        raise _unavailable(provider, route, SafeRouteReasonCode.ROUTE_FAILED)
    connection = _api_connection(provider)
    if provider == "claude":
        return stream_claude_api_turn(
            api_key=connection.api_key,
            system=system,
            model=model,
            messages=messages,
            executor=executor,
            tool_schemas=anthropic_tool_schemas(catalog),
            correlation_id=correlation_id,
        )
    return stream_openai_compatible_turn(
        provider=provider,
        api_url=_require_api_url(provider, connection),
        api_key=connection.api_key,
        model=model,
        system=system,
        messages=messages,
        executor=executor,
        tool_schemas=openai_tool_schemas(catalog),
        correlation_id=correlation_id,
    )


async def _stream_grok_cli_tool_turn(
    *,
    model: str | None,
    system: str,
    messages: list[dict[str, str]],
    executor: ToolExecutor,
    catalog: ToolCatalog,
    correlation_id: str | None,
) -> AsyncIterator[StreamEvent]:
    """Run Grok's text protocol through the shared tool loop."""
    async for event in _stream_cli_tool_turn(
        provider="grok",
        system=system,
        messages=messages,
        executor=executor,
        transport=GrokCliToolTransport(model=model, catalog=catalog),
        correlation_id=correlation_id,
    ):
        yield event


async def _stream_codex_cli_tool_turn(
    *,
    model: str | None,
    system: str,
    messages: list[dict[str, str]],
    executor: ToolExecutor,
    catalog: ToolCatalog,
    correlation_id: str | None,
) -> AsyncIterator[StreamEvent]:
    """Run Codex's text protocol through the shared tool loop."""
    async for event in _stream_cli_tool_turn(
        provider="codex",
        system=system,
        messages=messages,
        executor=executor,
        transport=CodexCliToolTransport(model=model, catalog=catalog),
        correlation_id=correlation_id,
    ):
        yield event


async def _stream_cli_tool_turn(
    *,
    provider: str,
    system: str,
    messages: list[dict[str, str]],
    executor: ToolExecutor,
    transport: ToolTransport,
    correlation_id: str | None,
) -> AsyncIterator[StreamEvent]:
    """Run one CLI text-protocol transport through the authorized tool loop."""
    try:
        async for event in stream_tool_loop(
            provider=provider,
            route=ProviderRoute.CLI.value,
            system=system,
            messages=messages,
            transport=transport,
            executor=executor,
            tool_failure_message=normalize_route_failure(
                SafeRouteReasonCode.TOOL_EXECUTION_FAILED,
            ).message,
            correlation_id=correlation_id,
        ):
            yield event
    except ToolLoopLimitError as exc:
        raise _unavailable(
            provider, ProviderRoute.CLI, SafeRouteReasonCode.TOOL_LIMIT_EXCEEDED,
        ) from exc
    except ToolLoopProtocolError as exc:
        log.warning(
            "Tool protocol rejection provider=%s route=cli code=%s",
            provider,
            ToolProtocolRejectionCode.TOOL_LOOP_TERMINAL_INVALID,
        )
        raise _unavailable(
            provider, ProviderRoute.CLI, SafeRouteReasonCode.TOOL_PROTOCOL_ERROR,
        ) from exc


def call_provider_once(
    *, provider: str, model: str | None, prompt: str, timeout: int, system: str | None = None,
) -> str:
    """Run one tool-free completion.

    Grok and Codex use their HTTP API. Claude uses its API with an Anthropic
    key and its CLI without one; only the CLI runs without a model.
    """
    if provider not in ProviderName:
        raise _unavailable(provider, ProviderRoute.API, SafeRouteReasonCode.ROUTE_FAILED)
    refuse_empty_model(provider, ProviderRoute.API.value, model)
    if model is None and provider != "claude":
        raise _unavailable(provider, ProviderRoute.API, SafeRouteReasonCode.ROUTE_FAILED)
    try:
        if provider == "claude":
            return call_claude_once(model=model, prompt=prompt, timeout=timeout, system=system)
        connection = _api_connection(provider)
        return call_openai_compatible_once(
            provider=provider,
            api_url=_require_api_url(provider, connection),
            api_key=connection.api_key,
            model=model,
            prompt=prompt, timeout=timeout, system=system,
        )
    except (ClaudeUnavailableError, HostWiringError):
        raise
    except ProviderUnavailableError as exc:
        if exc.reason is not None:
            raise
        raise _unavailable(provider, ProviderRoute.API, SafeRouteReasonCode.ROUTE_FAILED) from exc
    except Exception as exc:
        log.warning("One-shot completion failed provider=%s class=%s", provider, type(exc).__name__)
        raise _unavailable(provider, ProviderRoute.API, SafeRouteReasonCode.ROUTE_FAILED) from exc


def _api_connection(provider: str) -> _ApiConnection:
    config = current_config()
    if provider == "claude":
        api_key = _require_secret(provider, ProviderRoute.API, config.anthropic_api_key)
        return _ApiConnection(api_key)
    if provider == "grok":
        return _ApiConnection(
            _require_secret(provider, ProviderRoute.API, config.xai_api_key),
            GROK_CHAT_URL,
        )
    if provider == "codex":
        return _ApiConnection(
            _require_secret(provider, ProviderRoute.API, config.openai_api_key),
            OPENAI_CHAT_URL,
        )
    raise _unavailable(provider, ProviderRoute.API, SafeRouteReasonCode.ROUTE_FAILED)


def _require_api_url(provider: str, connection: _ApiConnection) -> str:
    if connection.api_url is None:
        raise _unavailable(provider, ProviderRoute.API, SafeRouteReasonCode.ROUTE_FAILED)
    return connection.api_url


def _require_secret(provider: str, route: ProviderRoute, secret) -> str:
    value = secret.get_secret_value() if secret is not None else ""
    if not value:
        raise _unavailable(provider, route, SafeRouteReasonCode.API_KEY_NOT_SET)
    return value


def _unavailable(
    provider: str,
    route: ProviderRoute,
    code: SafeRouteReasonCode,
) -> ProviderUnavailableError:
    return ProviderUnavailableError(provider, route.value, normalize_route_failure(code))
