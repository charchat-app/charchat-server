"""CREATE-US-08-AC04·AC11 로컬 개발자용 Jev HTTP 전송(테스트 전용, 운영 앱에 연결하지 않음).

- `DecisionsTransport` 구현이다. 운영 전송·마감 정책은 미정이므로 `tests/` 밖으로 내보내지 않는다.
- 자격 증명은 생성자로 명시적으로 주입한다. 이 모듈은 환경변수·`.env`를 읽지 않는다.
- 요청은 고정 Jev 엔드포인트(`ENDPOINT`)로만 보낸다. 다른 URL 요청은 보내지 않고 실패한다.
  리디렉션을 따르지 않으므로 bearer는 다른 호스트로 가지 않는다(3xx는 200이 아닌 상태로 돌려준다).
- `httpx.AsyncClient(trust_env=False, follow_redirects=False)`로 한 번만 보낸다. 재시도는 없다.
- `asyncio.timeout`이 연결·응답 헤더·본문 읽기 전체를 하나의 마감으로 묶는다(청크 간격이 아니라
  전체 시간). 본문은 압축 해제 없이 원시 바이트로 읽고 상한을 넘기는 순간 멈춘다.
- 200이 아닌 응답은 본문을 읽지 않고 상태만 돌려준다. 예상 밖 `Content-Encoding`, 길이 불일치
  (잘림), 프로토콜 오류, 시간 초과는 모두 원문·헤더·자격 증명을 담지 않은 `JevTransportError`다.
"""

import asyncio

import httpx

from app.features.creation.save_visibility.jev_decisions import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    DecisionsReply,
    DecisionsRequest,
)

DEFAULT_DEADLINE_SECONDS = 60.0
_IDENTITY = frozenset({"", "identity"})


class JevTransportError(Exception):
    """중립 전송 실패. 원인 예외·응답 원문·헤더·자격 증명을 담지 않는다."""

    def __init__(self):
        super().__init__("Jev 전송 실패")


class _Reject(Exception):
    """내부 거부 신호. 밖으로 나가기 전에 `JevTransportError`로 바뀐다."""


def _valid_key(api_key):
    return (
        type(api_key) is str
        and 0 < len(api_key) <= 512
        and api_key.isascii()
        and api_key.isprintable()
        and not any(ch.isspace() for ch in api_key)
    )


class JevHttpTransport:
    """명시적으로 주입한 키로 고정 Jev 엔드포인트에 한 번 POST한다. repr에 키를 싣지 않는다."""

    def __init__(
        self,
        api_key: str,
        *,
        deadline: float = DEFAULT_DEADLINE_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not _valid_key(api_key):
            raise ValueError("API 키 형식이 올바르지 않습니다.")  # 값은 싣지 않는다.
        if type(deadline) not in (int, float) or not 0 < deadline <= 300:
            raise ValueError("마감 시간이 올바르지 않습니다.")
        self._api_key = api_key
        self._deadline = float(deadline)
        self._transport = transport  # 테스트용 MockTransport 주입. 기본은 httpx 네트워크 전송.

    def __repr__(self):
        return "JevHttpTransport(...)"

    def send(self, request: DecisionsRequest) -> DecisionsReply:
        failed = False
        try:
            if type(request) is not DecisionsRequest or request.url != ENDPOINT:
                raise _Reject
            cap = request.max_response_bytes
            if type(cap) is not int or not 0 < cap <= MAX_RESPONSE_BYTES:
                raise _Reject
            if type(request.body) is not bytes:
                raise _Reject
            return asyncio.run(self._send(request.body, cap))
        except Exception:
            failed = True
        # except 블록 밖에서 올려 원인 예외(__context__)가 붙지 않게 한다.
        if failed:
            raise JevTransportError()
        raise AssertionError("도달할 수 없음")

    async def _send(self, body: bytes, cap: int) -> DecisionsReply:
        async with asyncio.timeout(self._deadline):
            async with httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(None),  # 마감은 바깥 asyncio.timeout 하나로만 정한다.
            ) as client:
                outgoing = client.build_request(
                    "POST",
                    ENDPOINT,
                    content=body,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                )
                response = await client.send(outgoing, stream=True)
                try:
                    if response.status_code != 200:
                        return DecisionsReply(response.status_code, b"")  # 본문을 읽지 않는다.
                    return DecisionsReply(200, await _read_bounded(response, cap))
                finally:
                    await response.aclose()


async def _read_bounded(response: httpx.Response, cap: int) -> bytes:
    encodings = response.headers.get_list("content-encoding")
    if any(value.strip().lower() not in _IDENTITY for value in encodings):
        raise _Reject
    declared = response.headers.get_list("content-length")
    expected = None
    if declared:
        if len(set(declared)) != 1 or not declared[0].isdigit():
            raise _Reject
        expected = int(declared[0])
        if expected > cap:
            raise _Reject  # 읽기 전에 거부한다.
    received = bytearray()
    async for chunk in response.aiter_raw():
        if len(received) + len(chunk) > cap:
            raise _Reject  # 버퍼에 붙이기 전에 상한을 확인하고 더 읽지 않는다.
        received += chunk
    if expected is not None and len(received) != expected:
        raise _Reject  # 잘림·초과
    return bytes(received)
