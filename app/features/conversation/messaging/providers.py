"""CHAT-US-02 응답 생성 공급자. 실제 모델·공급자는 팀 결정 전이라 설정으로 고른다.

- fake: 네트워크 없이 정해진 가짜 응답을 작은 조각으로 보낸다(기본, 로컬·테스트용).
- openai_compatible: `{base}/chat/completions`를 `stream: true`로 부르고 SSE `data:` 줄을 읽는다.
  새 런타임 의존성 없이 표준 라이브러리 urllib만 쓴다.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import settings

FAIL_BEFORE_MARKER = "#fail-before"
FAIL_AFTER_MARKER = "#fail-after"


class ProviderError(Exception):
    """공급자 호출·스트림 실패. code는 생성 행의 error_code로 남긴다."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass
class ProviderRequest:
    model: str
    messages: list[dict]
    # fake 공급자가 응답을 만들 때 쓰는 보조 정보. 실제 공급자는 messages만 보낸다.
    character_names: list[str] = field(default_factory=list)
    last_user_text: str = ""


class Provider(Protocol):
    def stream(self, request: ProviderRequest) -> Iterator[str]: ...


class FakeProvider:
    """첫 캐릭터 이름으로 입력 요약을 되돌려주는 결정적 가짜 공급자."""

    def __init__(self, delay_ms: int = 0, fail_mode: str = "none") -> None:
        self.delay_ms = delay_ms
        self.fail_mode = fail_mode

    def reply(self, request: ProviderRequest) -> str:
        summary = " ".join(request.last_user_text.split())[:40]
        lines = []
        if request.character_names:
            lines.append(
                f"@{request.character_names[0]}: (가짜 응답) *고개를 끄덕인다.* "
                f"\u201c{summary}\u201d라고 했지?"
            )
        lines.append(f"@나레이터: 가짜 모델({request.model})의 응답입니다.")
        return "\n".join(lines)

    def stream(self, request: ProviderRequest) -> Iterator[str]:
        mode = self.fail_mode
        if FAIL_BEFORE_MARKER in request.last_user_text:
            mode = "before"
        elif FAIL_AFTER_MARKER in request.last_user_text:
            mode = "after"
        if mode == "before":
            raise ProviderError("PROVIDER_ERROR", "가짜 공급자: 응답 전에 실패")
        text = self.reply(request)
        chunks = [text[i : i + 8] for i in range(0, len(text), 8)]
        for index, chunk in enumerate(chunks):
            if mode == "after" and index == 2:
                raise ProviderError("STREAM_INTERRUPTED", "가짜 공급자: 일부 응답 후 실패")
            if self.delay_ms:
                time.sleep(self.delay_ms / 1000)
            yield chunk


def iter_sse_content(lines: Iterable[bytes | str]) -> Iterator[str]:
    """OpenAI 호환 SSE 줄에서 delta.content만 순서대로 꺼낸다."""
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderError("STREAM_INTERRUPTED", "SSE 데이터 형식 오류") from exc
        if payload.get("error"):
            raise ProviderError("PROVIDER_ERROR", str(payload["error"]))
        for choice in payload.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                yield content


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def stream(self, request: ProviderRequest) -> Iterator[str]:
        body = json.dumps(
            {"model": request.model, "messages": request.messages, "stream": True}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        http_request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, headers=headers, method="POST"
        )
        try:
            response = urllib.request.urlopen(http_request, timeout=self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError("PROVIDER_ERROR", type(exc).__name__) from exc
        with response:
            try:
                yield from iter_sse_content(response)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ProviderError("STREAM_INTERRUPTED", type(exc).__name__) from exc


def get_provider() -> Provider:
    """현재 설정의 공급자. 테스트는 이 함수를 바꿔 요청 내용을 기록하거나 실패를 흉내 낸다."""
    if settings.chat_llm_provider == "openai_compatible":
        if not settings.chat_llm_base_url:
            raise ProviderError("PROVIDER_NOT_CONFIGURED", "CHAT_LLM_BASE_URL을 설정하세요.")
        return OpenAICompatibleProvider(
            settings.chat_llm_base_url,
            settings.chat_llm_api_key or None,
            settings.chat_llm_timeout_seconds,
        )
    return FakeProvider(settings.chat_fake_chunk_delay_ms, settings.chat_fake_fail_mode)
