"""Buffered, supervised API completions with freshly selected app credentials."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, aclosing
from http.cookiejar import CookieJar, DefaultCookiePolicy

from httpx import AsyncClient

from agent_runtime.errors import ProviderUnavailableError, SafeRouteReasonCode, raise_without_chain
from agent_runtime.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_runtime.openai_adapter import OpenAIConversation, complete_openai_compatible_text
from agent_runtime.tool_loop import InitialTurn
from agent_runtime_service.api_credentials import InvalidApiCredentials, read_api_credentials
from agent_runtime_service.api_tools import offered_schemas, require_unprotected, tool_conversation
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.relayed_events import RelayedEvents
from agent_runtime_service.routing import Api
from agent_runtime_service.run_liveness import RunClock
from agent_runtime_service.run_tools import RunTools
from agent_runtime_service.runners import AdmittedRun, ApiUnavailable


class ApiRunner:
    def __init__(
        self, config: ServiceConfig, *, tls_context: ssl.SSLContext | None, clock: RunClock,
    ) -> None:
        assert config.api is not None
        self._tls_context = tls_context
        self._clock = clock
        self._config = config.api
        self._apps = frozenset(config.apps)
        self._timeout = config.runs.max_duration_s
        self._verify: ssl.SSLContext | bool = True
        if config.api.ca_file is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cafile=str(config.api.ca_file))
            self._verify = context

    async def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]:
        assert isinstance(admitted.route, Api)
        try:
            credentials = read_api_credentials(self._config.credential_file, self._apps)
            key = credentials.for_app(admitted.app)
        except InvalidApiCredentials:
            key = None
        if key is None:
            raise_without_chain(ApiUnavailable())
        failure = None
        try:
            async with AsyncClient(
                trust_env=False, follow_redirects=False, timeout=self._timeout, verify=self._verify,
                cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
            ) as client:
                initial = InitialTurn(admitted.request.turn.system, [
                    {"role": message.role, "content": message.text}
                    for message in admitted.request.turn.messages
                ])
                require_unprotected([initial.system, initial.messages],
                                    key.get_secret_value(), admitted.capability)
                if admitted.request.tools:
                    async with aclosing(self._tool_events(
                        admitted, client, key.get_secret_value(), initial,
                    )) as events:
                        async for event in events:
                            yield event
                    return
                text = await complete_openai_compatible_text(
                    client=client, provider="api", api_url=self._config.completion_url,
                    api_key=key.get_secret_value(),
                    model=admitted.route.model if admitted.route.model is not None
                    else admitted.route.alias,
                    system=initial.system, messages=initial.messages,
                )
        except ProviderUnavailableError as error:
            assert error.reason is not None
            failure = error.reason.code
        except Exception:
            failure = SafeRouteReasonCode.API_PROTOCOL_ERROR
        if failure is SafeRouteReasonCode.API_HTTP_ERROR:
            raise_without_chain(ApiUnavailable())
        if failure is not None:
            raise_without_chain(ValueError("The API returned an invalid completion."))
        if key.get_secret_value() in text or (
            admitted.capability is not None and admitted.capability.appears_in(text.encode("utf-8"))
        ):
            raise_without_chain(ValueError("The API returned protected credentials."))
        if text:
            yield AssistantTextEvent(text=text)
        yield FinalEvent(text=text)

    async def _tool_events(
        self, admitted: AdmittedRun, client: AsyncClient, key: str, initial: InitialTurn,
    ) -> AsyncGenerator[StreamEvent, None]:
        assert isinstance(admitted.route, Api)
        assert admitted.capability is not None and self._tls_context is not None
        async with AsyncExitStack() as lifetime:
            tools = await RunTools.open_session(
                str(admitted.run_id), admitted.capability.claims.aud, admitted.capability,
                self._tls_context, self._clock,
            )
            # The close callback also owns failures before a merged source exists.
            core_lifetime = AsyncExitStack()
            lifetime.push_async_callback(tools.close, core_lifetime.aclose)
            schemas = await offered_schemas(tools, key, admitted.capability)
            conversation = OpenAIConversation(
                client=client, provider="api", api_url=self._config.completion_url,
                api_key=key, model=admitted.route.model if admitted.route.model is not None
                else admitted.route.alias, tool_schemas=schemas,
            )
            events = RelayedEvents(tool_conversation(
                conversation, initial, tools, {schema["function"]["name"] for schema in schemas},
                key, admitted.capability,
            ), tools.calls)
            core_lifetime.push_async_callback(events.close)
            async for event in events:
                require_unprotected(event.model_dump(mode="json"), key, admitted.capability)
                yield event
