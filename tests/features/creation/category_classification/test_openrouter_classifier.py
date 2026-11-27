"""CREATE-US-09-AC01·AC03: OpenRouter 분류기의 요청 입력과 응답 검증. 네트워크·DB 없이 돈다."""

import io
import json
import urllib.request
from http.client import HTTPResponse

import pytest

from app.features.creation.category_classification import classifier as module
from app.features.creation.category_classification.classifier import (
    CATEGORY_CODES,
    ENDPOINT,
    ClassificationError,
    OpenRouterClassifier,
)
from app.features.creation.category_classification.worker import ClassificationInput

VALUE = ClassificationInput("마법 학교 세계관", ("기숙사 사감", None))


def reply(content, **extra):
    return json.dumps(
        {"choices": [{"message": {"content": content}, "finish_reason": "stop", **extra}]}
    ).encode()


class Transport:
    def __init__(self, body: bytes):
        self.body = body
        self.calls = []

    def __call__(self, url, headers, data, timeout, limit):
        self.calls.append((url, headers, data, timeout, limit))
        return self.body


def make(body, **kwargs):
    transport = Transport(body)
    options = {"model": "vendor/model-a", "api_key": "test-key", "transport": transport}
    return OpenRouterClassifier(**{**options, **kwargs}), transport


def test_request_sends_only_world_and_character_descriptions_CREATE_US_09_AC01():
    classify, transport = make(reply('{"categories": ["sf_fantasy", "modern"]}'), timeout=7)
    assert classify(VALUE) == ("sf_fantasy", "modern")
    ((url, headers, data, timeout, limit),) = transport.calls
    assert url == ENDPOINT == "https://openrouter.ai/api/v1/chat/completions"
    assert headers == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    assert (timeout, limit) == (7, module.MAX_RESPONSE_BYTES)
    payload = json.loads(data)
    assert set(payload) == {"model", "messages", "stream", "temperature"}
    assert (payload["model"], payload["stream"], payload["temperature"]) == (
        "vendor/model-a",
        False,
        0,
    )
    system, user = payload["messages"]
    assert (system["role"], user["role"]) == ("system", "user")
    assert json.loads(user["content"]) == {
        "world_description": "마법 학교 세계관",
        "character_descriptions": ["기숙사 사감", None],
    }
    # 모든 코드와 BL/GL 판단 기준(성별만으로 고르지 않음)을 함께 안내한다.
    assert all(code in system["content"] for code in CATEGORY_CODES)
    assert "성별만으로" in system["content"] and "연애" in system["content"]


def test_category_codes_match_seed_CREATE_US_09_AC03():
    assert CATEGORY_CODES == frozenset(
        {
            "bl",
            "gl",
            "historical_martial",
            "horror_mystery",
            "modern",
            "romance",
            "romance_fantasy",
            "sf_fantasy",
            "simulation_rpg",
        }
    )


@pytest.mark.parametrize(
    "content",
    [
        '  {"categories": ["romance"]}\n',
        '{"categories": ["bl", "romance", "modern"]}',
    ],
)
def test_accepts_one_to_three_known_distinct_codes_CREATE_US_09_AC03(content):
    classify, _ = make(reply(content))
    assert classify(VALUE) == tuple(json.loads(content)["categories"])


@pytest.mark.parametrize(
    "body",
    [
        reply('{"categories": []}'),
        reply('{"categories": ["bl", "gl", "modern", "romance"]}'),
        reply('{"categories": ["romance", "romance"]}'),
        reply('{"categories": ["unknown"], "categories": ["romance"]}'),
        reply('{"categories": ["unknown"]}'),
        reply('{"categories": ["Romance"]}'),
        reply('{"categories": [1]}'),
        reply('{"categories": "romance"}'),
        reply('{"categories": ["romance"], "reason": "x"}'),
        reply('{"labels": ["romance"]}'),
        reply('["romance"]'),
        reply('```json\n{"categories": ["romance"]}\n```'),
        reply('결과: {"categories": ["romance"]}'),
        reply('{"categories": ["romance"]}{"categories": ["modern"]}'),
        reply("not json"),
        reply(None),
        reply('{"categories": ["romance"]}', finish_reason="length"),
        reply('{"categories": ["romance"]}', finish_reason=None),
        b'{"error":{},"choices":[{"finish_reason":"stop","message":{"content":'
        b'"{\\"categories\\":[\\"romance\\"]}"}}]}',
        json.dumps({"choices": []}).encode(),
        json.dumps({"choices": [{"message": {}}, {"message": {}}]}).encode(),
        json.dumps({"error": {"message": "rate limited"}}).encode(),
        b"\xff\xfe",
        b"<html>",
    ],
)
def test_rejects_invalid_output_CREATE_US_09_AC03(body):
    classify, _ = make(body)
    with pytest.raises(ClassificationError):
        classify(VALUE)


@pytest.mark.parametrize(
    "kwargs",
    [{"model": ""}, {"model": " "}, {"api_key": ""}, {"timeout": 0}, {"timeout": -1}],
)
def test_requires_explicit_model_key_and_positive_timeout(kwargs):
    with pytest.raises(ValueError):
        make(reply('{"categories": ["romance"]}'), **kwargs)


def test_transport_errors_become_classification_errors():
    def failing(*_):
        raise OSError("connection reset")

    classify = OpenRouterClassifier(model="m", api_key="k", transport=failing)
    with pytest.raises(ClassificationError):
        classify(VALUE)


def test_response_cap_rejects_oversized_body():
    assert module._read_capped(io.BytesIO(b"x" * 8), 8) == b"x" * 8
    with pytest.raises(ClassificationError):
        module._read_capped(io.BytesIO(b"x" * 9), 8)


def test_default_transport_never_follows_redirects():
    handler = module._NoRedirect()
    request = urllib.request.Request(ENDPOINT, data=b"{}", method="POST")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://evil.test") is None
    opener = module._opener()
    assert any(isinstance(h, module._NoRedirect) for h in opener.handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in opener.handlers)


def test_default_transport_rejects_non_fixed_origin():
    with pytest.raises(ClassificationError):
        module.urllib_transport("http://openrouter.ai/api/v1/chat/completions", {}, b"", 1, 10)


@pytest.mark.parametrize(
    "length,valid", [(None, True), ("2", True), ("invalid", False), ("3", False)]
)
def test_transport_rejects_malformed_or_truncated_length(monkeypatch, length, valid):
    header = b"" if length is None else f"Content-Length: {length}\r\n".encode()

    class Socket:
        def makefile(self, _):
            return io.BytesIO(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n{}")

    class Response(HTTPResponse):
        def geturl(self):
            return ENDPOINT

    response = Response(Socket())
    response.begin()

    class Opener:
        def open(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(module, "_opener", Opener)
    if valid:
        assert module.urllib_transport(ENDPOINT, {}, b"", 1, 10) == b"{}"
    else:
        with pytest.raises(ClassificationError):
            module.urllib_transport(ENDPOINT, {}, b"", 1, 10)
