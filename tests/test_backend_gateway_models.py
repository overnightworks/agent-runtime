"""``list_gateway_models`` against a scripted gateway: the cells of M-M."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx2
import pytest

from agent_runtime_client.agents import AgentDefinition
from agent_runtime_client.backend.fake_gateway import FakeGateway, Reply, model_list
from agent_runtime_client.backend.gateway import (
    GatewayModels,
    ModelListUnavailable,
    list_gateway_models,
)
from agent_runtime_client.config import ChatGateway
from agent_runtime_client.turn import MAX_EVENT_BYTES
from tests.support.gateway_turn_support import (
    GATEWAY_KEY,
    FailingTransport,
    HangingTransport,
    SkewedLoop,
    a_gateway_config,
)

DEADLINE_SECONDS = 10


def a_gateway(directory: Path) -> ChatGateway:
    return a_gateway_config(directory).gateway


def an_agent(*, picture_size: str | None = None) -> AgentDefinition:
    return AgentDefinition(
        name="painter" if picture_size else "writer",
        description="An agent",
        roles=["admin", "user"],
        tools=(),
        prompt="Do it.",
        model="image/standin" if picture_size else "text/standin",
        max_duration_seconds=30,
        max_tool_calls=None,
        picture_size=picture_size,
    )


def list_models(gateway: ChatGateway, transport: httpx2.AsyncBaseTransport) -> GatewayModels:
    with asyncio.Runner(loop_factory=SkewedLoop) as runner:
        return runner.run(list_gateway_models(gateway, transport=transport))


def the_failure(gateway: ChatGateway, transport: httpx2.AsyncBaseTransport) -> ModelListUnavailable:
    with pytest.raises(ModelListUnavailable) as caught:
        list_models(gateway, transport)
    return caught.value


def everything_it_shows(failure: ModelListUnavailable) -> str:
    chain, link = [], failure
    while link is not None:
        chain.append(f"{link!s} {link.args!r} {link!r}")
        link = link.__cause__ or link.__context__
    return " ".join(chain)


def a_json_reply(body: object, status: int = 200) -> Reply:
    return Reply(status=status, body=json.dumps(body).encode())


def a_padded_reply(total_bytes: int) -> Reply:
    base = {"data": [{"id": "text/standin"}], "pad": ""}
    padding = total_bytes - len(json.dumps(base).encode())
    return a_json_reply({**base, "pad": "x" * padding})


def test_the_request_is_a_bare_get_with_the_key_and_nothing_else(tmp_path):
    fake = FakeGateway(model_list("text/standin"))

    list_models(a_gateway(tmp_path), fake)

    (sent,) = fake.requests
    assert (sent.method, sent.url.path, sent.url.query, sent.content) == (
        "GET",
        "/v1/models",
        b"",
        b"",
    )
    assert sent.headers["authorization"] == f"Bearer {GATEWAY_KEY}"
    assert [name for name in sent.headers if name.startswith(("anthropic-", "x-litellm-"))] == []
    assert "content-type" not in sent.headers


def test_the_list_splits_text_and_image_names_in_the_gateways_order(tmp_path):
    fake = FakeGateway(model_list("text/b", "image/y", "text/a", "image/x"))

    models = list_models(a_gateway(tmp_path), fake)

    assert (models.text, models.image) == (("text/b", "text/a"), ("image/y", "image/x"))


@pytest.mark.parametrize("other", ["audio/tts", "Text/Opus", "text/", "gpt-4o", "text/a b", ""])
def test_an_id_of_another_kind_is_not_offered_and_is_no_error(tmp_path, other):
    fake = FakeGateway(model_list("text/a", other, "image/x"))

    models = list_models(a_gateway(tmp_path), fake)

    assert (models.text, models.image) == (("text/a",), ("image/x",))


def test_an_empty_list_offers_nothing(tmp_path):
    models = list_models(a_gateway(tmp_path), FakeGateway(model_list()))

    assert (models.text, models.image) == ((), ())


def test_for_agent_answers_the_names_of_that_agents_kind(tmp_path):
    models = list_models(a_gateway(tmp_path), FakeGateway(model_list("text/a", "image/x")))

    assert (
        models.for_agent(an_agent()),
        models.for_agent(an_agent(picture_size="1024x1024")),
    ) == (("text/a",), ("image/x",))


def test_the_key_is_read_again_for_each_call(tmp_path):
    gateway = a_gateway(tmp_path)
    fake = FakeGateway(model_list("text/a"))
    list_models(gateway, fake)
    gateway.key_file.write_text("second-key-0002\n")

    list_models(gateway, fake)

    assert [request.headers["authorization"] for request in fake.requests] == [
        f"Bearer {GATEWAY_KEY}",
        "Bearer second-key-0002",
    ]


@pytest.mark.parametrize(
    ("transport", "detail"),
    [
        (FakeGateway(Reply(body=b"not json")), "malformed_reply"),
        (FakeGateway(Reply(body=b"")), "malformed_reply"),
        (FakeGateway(a_json_reply({"object": "list"})), "malformed_reply"),
        (FakeGateway(a_json_reply({"data": {"id": "text/a"}})), "malformed_reply"),
        (FakeGateway(a_json_reply({"data": ["text/a"]})), "malformed_reply"),
        (FakeGateway(a_json_reply({"data": [{"name": "text/a"}]})), "malformed_reply"),
        (FakeGateway(a_json_reply({"data": [{"id": 7}]})), "malformed_reply"),
        (FakeGateway(a_json_reply([])), "malformed_reply"),
        (FakeGateway(Reply(body=b"\xff" + GATEWAY_KEY.encode())), "malformed_reply"),
        (FakeGateway(a_json_reply({"error": GATEWAY_KEY}, status=401)), "gateway_refused"),
        (FakeGateway(a_json_reply({"error": GATEWAY_KEY}, status=429)), "gateway_refused"),
        (FakeGateway(a_json_reply({"error": GATEWAY_KEY}, status=500)), "gateway_failed"),
        (
            FakeGateway(Reply(status=302, headers={"Location": "http://elsewhere.test/v1/models"})),
            "gateway_failed",
        ),
        (FakeGateway(a_padded_reply(MAX_EVENT_BYTES + 1)), "malformed_reply"),
        (FailingTransport(), "connection_failed"),
        (HangingTransport(seconds_passing=DEADLINE_SECONDS + 1), "timed_out"),
    ],
    ids=[
        "not-json",
        "empty-body",
        "no-data",
        "data-not-a-list",
        "entry-not-an-object",
        "entry-without-id",
        "id-not-text",
        "reply-not-an-object",
        "invalidly-encoded",
        "401",
        "429",
        "500",
        "redirect",
        "body-over-its-bound",
        "connection-lost",
        "stall",
    ],
)
def test_every_failure_raises_the_one_exception_without_the_key(
    tmp_path, caplog, transport, detail
):
    caplog.set_level(logging.DEBUG)

    failure = the_failure(a_gateway(tmp_path), transport)

    assert failure.detail == detail
    assert GATEWAY_KEY not in everything_it_shows(failure)
    assert GATEWAY_KEY not in caplog.text


def test_an_origin_the_http_library_refuses_raises_the_one_exception(tmp_path):
    gateway = ChatGateway(origin="http://999.999.999.999", key_file=a_gateway(tmp_path).key_file)

    listing = list_gateway_models(gateway)

    with pytest.raises(ModelListUnavailable) as caught:
        asyncio.run(listing)

    assert caught.value.detail == "connection_failed"


def test_the_raising_frame_and_its_callers_hold_no_key_in_a_local(tmp_path):
    failure = the_failure(a_gateway(tmp_path), FailingTransport())

    frames, trace = [], failure.__traceback__
    while trace is not None:
        frames.append(trace.tb_frame)
        trace = trace.tb_next
    held = [repr(value) for frame in frames for value in frame.f_locals.values()]
    assert [text for text in held if GATEWAY_KEY in text] == []


def test_a_redirect_is_not_followed(tmp_path):
    redirect = Reply(status=302, headers={"Location": "http://elsewhere.test/v1/models"})
    fake = FakeGateway(redirect, model_list("text/a"))

    the_failure(a_gateway(tmp_path), fake)

    assert len(fake.requests) == 1


def test_a_stalled_call_is_cancelled(tmp_path):
    hanging = HangingTransport(seconds_passing=DEADLINE_SECONDS + 1)

    the_failure(a_gateway(tmp_path), hanging)

    assert hanging.cancelled


def test_a_body_at_its_bound_passes(tmp_path):
    models = list_models(a_gateway(tmp_path), FakeGateway(a_padded_reply(MAX_EVENT_BYTES)))

    assert models.text == ("text/standin",)


@pytest.mark.parametrize(
    "content",
    [None, b"", b"has space\n", b"first-line-0001\nsecond\n", b"k" * 2000],
    ids=["missing", "empty", "space", "two-lines", "oversized"],
)
def test_a_key_file_that_is_missing_or_malformed_raises_the_same_and_sends_nothing(
    tmp_path, caplog, content
):
    caplog.set_level(logging.DEBUG)
    gateway = a_gateway(tmp_path)
    if content is None:
        gateway.key_file.unlink()
    else:
        gateway.key_file.write_bytes(content)
    fake = FakeGateway(model_list("text/a"))

    failure = the_failure(gateway, fake)

    assert (failure.detail, fake.requests) == ("key_unusable", [])
    assert "first-line-0001" not in everything_it_shows(failure) + caplog.text


def test_a_list_call_keeps_the_transport_loggers_off_debug_so_the_key_stays_out_of_them(
    tmp_path, caplog
):
    for name in ("httpx2", "httpcore2"):
        caplog.set_level(logging.DEBUG, logger=name)

    list_models(a_gateway(tmp_path), FakeGateway(model_list("text/a")))

    assert not any(
        logging.getLogger(name).isEnabledFor(logging.DEBUG)
        for name in ("httpx2", "httpcore2", "httpcore2.connection", "httpcore2.http11")
    )
