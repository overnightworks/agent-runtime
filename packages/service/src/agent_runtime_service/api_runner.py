"""One buffered, supervised API text completion with freshly selected app credentials."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator

from httpx import AsyncClient

from agent_runtime.errors import ProviderUnavailableError, SafeRouteReasonCode
from agent_runtime.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_runtime.openai_adapter import complete_openai_compatible_text
from agent_runtime_service.api_credentials import InvalidApiCredentials, read_api_credentials
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.routing import Api
from agent_runtime_service.runners import AdmittedRun, ApiUnavailable


class ApiRunner:
    def __init__(self, config: ServiceConfig) -> None:
        assert config.api is not None
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
        if admitted.request.tools:
            raise ValueError("The API route does not support tools.")
        try:
            credentials = read_api_credentials(self._config.credential_file, self._apps)
            key = credentials.for_app(admitted.app)
        except InvalidApiCredentials:
            key = None
        if key is None:
            raise ApiUnavailable()
        failure = None
        try:
            async with AsyncClient(
                trust_env=False, follow_redirects=False, timeout=self._timeout, verify=self._verify,
            ) as client:
                text = await complete_openai_compatible_text(
                    client=client, provider="api", api_url=self._config.completion_url,
                    api_key=key.get_secret_value(),
                    model=admitted.route.model if admitted.route.model is not None
                    else admitted.route.alias,
                    system=admitted.request.turn.system,
                    messages=[{"role": message.role, "content": message.text}
                              for message in admitted.request.turn.messages],
                )
        except ProviderUnavailableError as error:
            assert error.reason is not None
            failure = error.reason.code
        if failure is SafeRouteReasonCode.API_HTTP_ERROR:
            raise ApiUnavailable()
        if failure is not None:
            raise ValueError("The API returned an invalid completion.")
        if key.get_secret_value() in text or (
            admitted.capability is not None and admitted.capability.appears_in(text.encode("utf-8"))
        ):
            raise ValueError("The API returned protected credentials.")
        if text:
            yield AssistantTextEvent(text=text)
        yield FinalEvent(text=text)
