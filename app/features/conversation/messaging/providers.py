"""CHAT-US-02 응답 생성 공급자. 세션 모델(models 행)의 provider·provider_model_id로 호출한다.

- fake: 네트워크 없이 정해진 가짜 응답을 작은 조각으로 보낸다(기본, 로컬·테스트용).
  모델의 provider_model_id는 응답 문구의 이름표로만 쓴다.
- openai_compatible: provider가 'openai'인 모델을 `{CHAT_LLM_BASE_URL}/chat/completions`
  (기본 https://api.openai.com/v1)로 `stream: true` 호출하고 SSE `data:` 줄을 읽는다.
  키는 CHAT_LLM_API_KEY에서만 읽는다. 새 런타임 의존성 없이 표준 라이브러리 urllib만 쓴다.

CHAT-US-13: 공급자는 응답이 알려 준 실제 모델 ID를 `ProviderRequest.report_model`로 남긴다
(OpenAI 호환은 SSE payload의 `model`, fake는 `request.model`). 처음 받은 값만 기억한다.
다른 모델로 다시 호출(fallback)하지 않는다. 실패는 그대로 ProviderError로 올린다.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import settings

FAIL_BEFORE_MARKER = "#fail-before"
FAIL_AFTER_MARKER = "#fail-after"
# 실제 호출을 지원하는 models.provider 값.
SUPPORTED_PROVIDERS = ("openai",)
MODEL_OUTPUT_LIMIT = "MODEL_OUTPUT_LIMIT"


class ProviderError(Exception):
    """공급자 호출·스트림 실패. code는 생성 행의 error_code로 남긴다."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


# 공급자가 알려 준 실제 모델 ID의 최대 길이(CHAT-US-13). 넘으면 기록하지 않는다.
REPORTED_MODEL_MAX_LENGTH = 200


@dataclass
class ProviderRequest:
    # 공급자 쪽 모델 ID(models.provider_model_id)와 공급자(models.provider).
    model: str
    messages: list[dict]
    provider: str = "openai"
    # fake 공급자가 응답을 만들 때 쓰는 보조 정보. 실제 공급자는 messages만 보낸다.
    character_names: list[str] = field(default_factory=list)
    last_user_text: str = ""
    # 공급자가 응답에 실어 보낸 실제 모델 ID(CHAT-US-13). 알려 주지 않으면 None.
    reported_model: str | None = None

    def report_model(self, model: object) -> None:
        """처음 받은 쓸 수 있는 문자열만 기억한다.

        NUL이 든 값(PostgreSQL text에 저장할 수 없음)이나 지나치게 긴 값은 기록하지 않는다.
        """
        if (
            self.reported_model is None
            and isinstance(model, str)
            and model
            and "\x00" not in model
            and len(model) <= REPORTED_MODEL_MAX_LENGTH
        ):
            self.reported_model = model


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
            # 실제 공급자처럼 첫 조각과 함께 응답 모델을 알려 준다(요청 모델 그대로).
            request.report_model(request.model)
            yield chunk


def iter_sse_content(
    lines: Iterable[bytes | str], on_model: Callable[[object], None] | None = None
) -> Iterator[str]:
    """OpenAI 호환 SSE 줄에서 delta.content만 순서대로 꺼낸다.

    on_model이 있으면 payload마다 `model` 값을 넘긴다(CHAT-US-13 실제 모델 기록).

    `data: [DONE]` 또는 choice의 `finish_reason`을 받아야 정상 완료로 본다. 완료 신호 없이
    스트림이 끝나면(연결 끊김·프록시 절단 등) 응답이 잘렸을 수 있으므로 STREAM_INTERRUPTED로
    실패시킨다. 이미 보낸 조각이 있으면 작업이 failed_partial, 없으면 failed로 기록하고
    청구하지 않는다.
    """
    finished = False
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
        if on_model is not None and isinstance(payload, dict) and payload.get("model"):
            on_model(payload["model"])
        if payload.get("error"):
            raise ProviderError("PROVIDER_ERROR", str(payload["error"]))
        for choice in payload.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                yield content
            if choice.get("finish_reason") == "length":
                # 모델이 출력 한도에 도달한 경우. 전송 단절이나 정상 완료와 구분한다.
                raise ProviderError(MODEL_OUTPUT_LIMIT)
            if choice.get("finish_reason"):
                finished = True
    if not finished:
        raise ProviderError("STREAM_INTERRUPTED", "완료 신호 없이 스트림이 끝났습니다.")


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def stream(self, request: ProviderRequest) -> Iterator[str]:
        if request.provider not in SUPPORTED_PROVIDERS:
            raise ProviderError(
                "PROVIDER_NOT_CONFIGURED", f"지원하지 않는 공급자입니다: {request.provider}"
            )
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
                yield from iter_sse_content(response, on_model=request.report_model)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ProviderError("STREAM_INTERRUPTED", type(exc).__name__) from exc


def get_provider(timeout: float | None = None) -> Provider:
    """현재 설정의 공급자. 테스트는 이 함수를 바꿔 요청 내용을 기록하거나 실패를 흉내 낸다.

    timeout을 주면 그 대기 시간(초)으로 만든다(CHAT-US-08 장면 판단).
    """
    if settings.chat_llm_provider == "openai_compatible":
        if not settings.chat_llm_base_url:
            raise ProviderError("PROVIDER_NOT_CONFIGURED", "CHAT_LLM_BASE_URL을 설정하세요.")
        if not settings.chat_llm_api_key:
            raise ProviderError("PROVIDER_NOT_CONFIGURED", "CHAT_LLM_API_KEY를 설정하세요.")
        return OpenAICompatibleProvider(
            settings.chat_llm_base_url,
            settings.chat_llm_api_key,
            settings.chat_llm_timeout_seconds if timeout is None else timeout,
        )
    return FakeProvider(settings.chat_fake_chunk_delay_ms, settings.chat_fake_fail_mode)
