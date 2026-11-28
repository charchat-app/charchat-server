"""CREATE-US-08-AC04·AC11 로컬 개발자용 Jev HTTP 전송 계약(네트워크 없음).

`httpx.MockTransport`와 비동기 스트림으로 마감·상한·리디렉션·키 비노출·중립 실패를 확인한다.
마지막 두 테스트는 기존 Jev 어댑터를 주입한 실제 저장 API·PostgreSQL 종단 확인이다(DB 필요).
"""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.features.creation.save_visibility.jev_decisions import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    DecisionsReply,
    DecisionsRequest,
    JevDecisionsEvaluator,
)
from app.features.creation.save_visibility.jev_http_transport import (
    JevHttpTransport,
    JevTransportError,
)
from app.features.creation.save_visibility.review_verdict import (
    Eligibility,
    EligibilityReason,
    EligibilityVerdict,
    unavailable,
)
from tests.features.creation.save_visibility.test_jev_decisions import (
    encode,
    snap,
    valid_answers,
)
from tests.features.creation.save_visibility.test_jev_decisions_api import jev  # noqa: F401
from tests.features.creation.save_visibility.test_rating_review_api import rating_view

R = EligibilityReason
KEY = "sk-or-synthetic-must-not-leak"
BODY = b'{"model":"m"}'


class Recorder:
    """MockTransport 핸들러. 받은 요청(URL·헤더 사본)을 기록하고 `respond`의 응답을 돌려준다."""

    def __init__(self, respond):
        self.seen = []
        self.respond = respond

    async def __call__(self, request):
        self.seen.append(
            SimpleNamespace(
                url=str(request.url), headers=dict(request.headers), body=await request.aread()
            )
        )
        result = self.respond(request)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result


class Chunks(httpx.AsyncByteStream):
    """청크마다 `delay`초 쉬고 보낸다. 몇 개를 실제로 내보냈는지 센다."""

    def __init__(self, chunk=b"x" * 1024, count=None, delay=0.0):
        self.chunk, self.count, self.delay, self.sent = chunk, count, delay, 0

    async def __aiter__(self):
        while self.count is None or self.sent < self.count:
            await asyncio.sleep(self.delay)
            self.sent += 1
            yield self.chunk


def streamed(body, headers=None):
    """실제 네트워크처럼 아직 읽지 않은 스트림 응답(Content-Length 포함)."""
    headers = {"Content-Length": str(len(body)), **(headers or {})}
    return httpx.Response(200, headers=headers, stream=Chunks(chunk=body, count=1))


def transport_for(respond, deadline=5.0):
    handler = Recorder(respond)
    return JevHttpTransport(KEY, deadline=deadline, transport=httpx.MockTransport(handler)), handler


def request(cap=MAX_RESPONSE_BYTES, url=ENDPOINT, body=BODY):
    return DecisionsRequest(url, body, cap)


def assert_neutral(error):
    assert type(error) is JevTransportError
    assert error.__cause__ is None and error.__context__ is None
    assert KEY not in repr(error) and KEY not in str(error)
    assert str(error) == "Jev 전송 실패"


def fails(transport, req=None):
    with pytest.raises(JevTransportError) as caught:
        transport.send(req or request())
    assert_neutral(caught.value)


# --- 정상 전송 -----------------------------------------------------------------------------------


def test_posts_once_to_exact_endpoint_with_injected_bearer_CREATE_US_08_AC04():
    transport, handler = transport_for(lambda r: streamed(b'{"answers":{}}'))
    reply = transport.send(request())
    assert reply == DecisionsReply(200, b'{"answers":{}}')
    [seen] = handler.seen
    assert seen.url == ENDPOINT
    assert seen.headers["authorization"] == f"Bearer {KEY}"
    assert seen.headers["accept-encoding"] == "identity"
    assert seen.headers["content-type"] == "application/json"
    assert KEY not in repr(transport)


def test_reads_identity_body_in_many_quick_chunks_within_deadline_CREATE_US_08_AC04():
    stream = Chunks(chunk=b"a" * 100, count=50)
    transport, _ = transport_for(lambda r: httpx.Response(200, stream=stream), deadline=2)
    assert transport.send(request()).body == b"a" * 5000


@pytest.mark.parametrize("value", ["", "sk or", "sk-\n", "키", None, 1])
def test_key_must_be_explicit_and_header_safe_without_echo_CREATE_US_08_AC04(value):
    with pytest.raises(ValueError) as caught:
        JevHttpTransport(value)
    assert str(caught.value) == "API 키 형식이 올바르지 않습니다."


# --- 엔드포인트·리디렉션 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/api/alpha/decisions",
        "http://openrouter.ai/api/alpha/decisions",
        ENDPOINT + "/",
        ENDPOINT + "?x=1",
    ],
)
def test_other_urls_are_never_sent_CREATE_US_08_AC04(url):
    transport, handler = transport_for(lambda r: streamed(b"{}"))
    fails(transport, request(url=url))
    assert handler.seen == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_not_followed_and_bearer_stays_on_endpoint_CREATE_US_08_AC04(status):
    stream = Chunks()
    transport, handler = transport_for(
        lambda r: httpx.Response(
            status, headers={"Location": "https://evil.example/steal"}, stream=stream
        )
    )
    assert transport.send(request()) == DecisionsReply(status, b"")
    assert [s.url for s in handler.seen] == [ENDPOINT]
    assert stream.sent == 0  # 리디렉션 본문도 읽지 않는다.


def test_redirect_is_outage_through_adapter_CREATE_US_08_AC11():
    transport, handler = transport_for(
        lambda r: httpx.Response(307, headers={"Location": "https://evil.example/"})
    )
    verdict = JevDecisionsEvaluator(transport).evaluate(1, snap())
    assert verdict == unavailable(R.EVALUATOR_FAILURE)
    assert len(handler.seen) == 1


# --- 200이 아닌 응답 -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_non_200_returns_status_without_reading_large_body_CREATE_US_08_AC11(status):
    stream = Chunks()  # 끝없는 본문
    transport, handler = transport_for(lambda r: httpx.Response(status, stream=stream), deadline=1)
    assert transport.send(request()) == DecisionsReply(status, b"")
    assert stream.sent == 0
    assert len(handler.seen) == 1  # 재시도하지 않는다.


# --- 마감 ----------------------------------------------------------------------------------------


def test_deadline_covers_whole_body_not_per_chunk_CREATE_US_08_AC11():
    """청크마다 빨리 오더라도 전체가 마감을 넘기면 실패한다(청크 간 유휴 시간 기준이 아니다)."""
    stream = Chunks(chunk=b"a", count=10_000, delay=0.01)
    transport, _ = transport_for(lambda r: httpx.Response(200, stream=stream), deadline=0.3)
    started = time.monotonic()
    fails(transport)
    assert time.monotonic() - started < 2
    assert 0 < stream.sent < 10_000


def test_deadline_covers_idle_body_CREATE_US_08_AC11():
    stream = Chunks(count=1, delay=30)
    transport, _ = transport_for(lambda r: httpx.Response(200, stream=stream), deadline=0.2)
    started = time.monotonic()
    fails(transport)
    assert time.monotonic() - started < 2


def test_deadline_covers_response_headers_CREATE_US_08_AC11():
    async def slow_headers(request):
        await asyncio.sleep(30)
        return streamed(b"{}")

    transport, handler = transport_for(slow_headers, deadline=0.2)
    started = time.monotonic()
    fails(transport)
    assert time.monotonic() - started < 2
    assert len(handler.seen) == 1


# --- 상한·인코딩·잘림·프로토콜 -------------------------------------------------------------------


def test_streamed_body_over_cap_stops_reading_CREATE_US_08_AC04():
    stream = Chunks(chunk=b"x" * 1024)  # Content-Length 없는 끝없는 본문
    transport, _ = transport_for(lambda r: httpx.Response(200, stream=stream))
    fails(transport, request(cap=4096))
    assert stream.sent == 5  # 상한(4 KiB)을 넘긴 청크에서 멈춘다.


def test_body_exactly_at_cap_is_accepted_CREATE_US_08_AC04():
    transport, _ = transport_for(lambda r: streamed(b"x" * 4096))
    assert len(transport.send(request(cap=4096)).body) == 4096


def test_declared_length_over_cap_is_rejected_before_reading_CREATE_US_08_AC04():
    stream = Chunks(count=1)
    transport, _ = transport_for(
        lambda r: httpx.Response(200, headers={"Content-Length": "999999"}, stream=stream)
    )
    fails(transport, request(cap=4096))
    assert stream.sent == 0


@pytest.mark.parametrize("cap", [0, -1, MAX_RESPONSE_BYTES + 1])
def test_invalid_cap_is_not_sent_CREATE_US_08_AC04(cap):
    transport, handler = transport_for(lambda r: streamed(b"{}"))
    fails(transport, request(cap=cap))
    assert handler.seen == []


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip", "zstd"])
def test_unexpected_content_encoding_is_rejected_CREATE_US_08_AC04(encoding):
    transport, _ = transport_for(lambda r: streamed(b"{}", {"Content-Encoding": encoding}))
    fails(transport)


def test_identity_content_encoding_is_accepted_CREATE_US_08_AC04():
    transport, _ = transport_for(lambda r: streamed(b"{}", {"Content-Encoding": "identity"}))
    assert transport.send(request()).body == b"{}"


@pytest.mark.parametrize("length", ["10", "1", "abc", "-1"])
def test_truncated_or_bad_length_is_rejected_CREATE_US_08_AC04(length):
    stream = Chunks(chunk=b"12345", count=1)
    transport, _ = transport_for(
        lambda r: httpx.Response(200, headers={"Content-Length": length}, stream=stream)
    )
    fails(transport)


@pytest.mark.parametrize(
    "error",
    [
        httpx.RemoteProtocolError("peer closed connection without sending complete message"),
        httpx.ConnectError(f"Bearer {KEY}"),  # 원인 메시지에 무엇이 있어도 싣지 않는다.
        httpx.ReadError("reset"),
        OSError("boom"),
    ],
)
def test_protocol_and_network_errors_map_to_neutral_failure_once_CREATE_US_08_AC11(error):
    transport, handler = transport_for(lambda r: error)
    fails(transport)
    assert len(handler.seen) == 1  # 재시도하지 않는다.


def test_mid_body_protocol_error_is_neutral_CREATE_US_08_AC11():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            raise httpx.RemoteProtocolError("incomplete chunked read")

    transport, _ = transport_for(lambda r: httpx.Response(200, stream=Broken()))
    fails(transport)


def test_transport_failure_is_outage_through_adapter_CREATE_US_08_AC11():
    transport, handler = transport_for(lambda r: streamed(b"x" * 70_000))
    verdict = JevDecisionsEvaluator(transport).evaluate(1, snap())
    assert verdict == unavailable(R.EVALUATOR_FAILURE)
    assert len(handler.seen) == 1


def test_proxy_environment_is_ignored_CREATE_US_08_AC04(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    transport, handler = transport_for(lambda r: streamed(b"{}"))
    assert transport.send(request()).body == b"{}"
    assert [s.url for s in handler.seen] == [ENDPOINT]


# --- 기존 어댑터 + 실제 저장 API·PostgreSQL(오프라인) ---------------------------------------------


def _jev_reply(request):
    """요청 질문에 맞는 적격 응답. 기존 계약 테스트의 응답 생성기를 그대로 쓴다."""
    decisions = SimpleNamespace(body=request.content)
    return streamed(encode({"answers": valid_answers(decisions)}))


class Counted:
    """`jev` 픽스처가 보는 `requests`를 HTTP 전송 위에 기록한다."""

    def __init__(self, inner):
        self.inner, self.requests = inner, []

    def send(self, request):
        self.requests.append(request)
        return self.inner.send(request)


def test_http_transport_through_save_api_applies_unsafe_CREATE_US_08_AC02_AC04(jev):  # noqa: F811
    transport, handler = transport_for(_jev_reply)
    counted = Counted(transport)
    ctx = jev(counted)
    assert rating_view(ctx) == ("UNSAFE", "completed", None)
    [seen] = handler.seen
    [adapter_request] = counted.requests
    assert seen.url == ENDPOINT
    # 어댑터가 만든 본문이 그대로 한 번 전송된다(로컬 ID만).
    assert seen.body == adapter_request.body
    assert [c["id"] for c in json.loads(seen.body)["state"]["characters"]] == ["c1"]


def test_http_transport_failure_through_save_api_keeps_safe_CREATE_US_08_AC11(jev):  # noqa: F811
    transport, handler = transport_for(lambda r: streamed(b"{}", {"Content-Encoding": "gzip"}))
    ctx = jev(Counted(transport))
    assert rating_view(ctx) == (
        "SAFE",
        "failed",
        {"outcome": "unavailable", "reasons": ["evaluator_failure"]},
    )
    assert len(handler.seen) == 1


def test_eligible_verdict_via_http_transport_offline_CREATE_US_08_AC04():
    transport, _ = transport_for(_jev_reply)
    verdict = JevDecisionsEvaluator(transport).evaluate(1, snap())
    assert verdict == EligibilityVerdict(outcome=Eligibility.ELIGIBLE, reasons=frozenset())
