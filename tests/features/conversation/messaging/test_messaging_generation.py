"""CHAT-US-02 응답 생성 진행·실패·재시도·롱 폴링."""

import io
import json
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import pytest
from messaging_helpers import GENERATIONS, PLAYER, SESSIONS, RecordingProvider, send, user_part
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import settings
from app.db import session as db_session
from app.features.conversation.messaging import providers, repository, service, worker
from app.features.conversation.messaging.providers import (
    FakeProvider,
    ProviderError,
    ProviderRequest,
    iter_sse_content,
)


def generation_row(db, generation_id):
    return db.execute(
        """SELECT g.status, g.text, g.billable, g.error_code, m.status AS message_status
           FROM conversation_generations g
           JOIN conversation_messages m
             ON m.session_id = g.session_id AND m.id = g.assistant_message_id
           WHERE g.id = %s""",
        (generation_id,),
    ).fetchone()


def retry(client, generation_id, request_id="retry-0001"):
    return client.post(
        f"{GENERATIONS}/{generation_id}/retry", headers=PLAYER, json={"request_id": request_id}
    )


def test_chat_us_02_ac15_failure_without_content_is_not_billed_and_retryable(
    client, db, session, monkeypatch
):
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "before")
    parts = [user_part("*숨을 고른다.*"), user_part("다시 말해 줘")]
    generation = send(client, session, parts).json()["id"]
    row = generation_row(db, generation)
    assert row == {
        "status": "failed",
        "text": "",
        "billable": False,
        "error_code": "PROVIDER_ERROR",
        # 팀 표의 assistant 메시지는 failed로 남고 이력에는 나오지 않는다.
        "message_status": "failed",
    }
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    failed = turns["last_failed_generation"]
    assert failed["id"] == generation
    assert failed["status"] == "failed"
    assert failed["retryable"] is True
    # AC16: 실패한 입력은 그대로 남아 다시 작성하지 않고 재시도할 수 있다.
    assert [p["body"] for p in turns["messages"][0]["parts"]] == [p["body"] for p in parts]
    assert [m["role"] for m in turns["messages"]] == ["user"]


def test_chat_us_02_ac15_ac16_retry_reuses_input_and_completes(client, db, session, monkeypatch):
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "before")
    failed = send(client, session, [user_part("재시도할 입력")]).json()
    # 자동 재호출 없음: 실패 후 생성 행은 하나뿐이다.
    assert db.execute("SELECT count(*) AS n FROM conversation_generations").fetchone()["n"] == 1
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "none")
    retried = retry(client, failed["id"])
    assert retried.status_code == 202
    body = retried.json()
    assert body["id"] != failed["id"]
    assert body["status"] == "pending"
    assert body["user_message_id"] == failed["user_message_id"]
    assert generation_row(db, body["id"])["status"] == "completed"
    # 같은 재시도 요청 ID는 같은 생성을 돌려준다.
    assert retry(client, failed["id"]).json()["id"] == body["id"]
    # 이제 최신이 아니므로 원래 실패 건은 다시 시도할 수 없다.
    again = retry(client, failed["id"], request_id="retry-0002")
    assert again.status_code == 409
    assert again.json()["code"] == "GENERATION_NOT_RETRYABLE"
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert [m["role"] for m in turns["messages"]] == ["user", "assistant"]
    assert turns["last_failed_generation"] is None


def test_chat_us_02_ac17_partial_failure_keeps_text_out_of_history(
    client, db, session, monkeypatch
):
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "after")
    generation = send(client, session, [user_part("중간에 끊김")]).json()["id"]
    row = generation_row(db, generation)
    assert row["status"] == "failed_partial"
    assert row["message_status"] == "interrupted"
    assert row["error_code"] == "STREAM_INTERRUPTED"
    assert row["text"] != ""
    assert row["billable"] is False
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert [m["role"] for m in turns["messages"]] == ["user"]
    assert turns["last_failed_generation"]["status"] == "failed_partial"
    assert turns["last_failed_generation"]["text"] == row["text"]
    assert turns["last_failed_generation"]["retryable"] is False
    response = retry(client, generation)
    assert response.status_code == 409
    assert response.json()["code"] == "GENERATION_NOT_RETRYABLE"


def test_chat_us_02_ac17_save_failure_is_distinguished(client, db, session, monkeypatch):
    original = repository.insert_parts

    def failing_insert(conn, session_id, message_id, parts):
        role = conn.execute(
            "SELECT role FROM conversation_messages WHERE session_id = %s AND id = %s",
            (session_id, message_id),
        ).fetchone()["role"]
        if role == "assistant":
            raise RuntimeError("저장 실패 흉내")
        return original(conn, session_id, message_id, parts)

    monkeypatch.setattr(repository, "insert_parts", failing_insert)
    generation = send(client, session, [user_part("저장 실패")]).json()["id"]
    row = generation_row(db, generation)
    assert row["status"] == "save_failed"
    assert row["error_code"] == "SAVE_FAILED"
    assert row["text"].startswith("@도경:")
    assert row["billable"] is False
    assert row["message_status"] == "failed"
    # 응답 부분은 하나도 저장되지 않는다(부분 저장과 완료 표시는 한 트랜잭션).
    assert db.execute(
        """SELECT count(*) AS n FROM conversation_message_parts p
           JOIN conversation_messages m ON m.session_id = p.session_id AND m.id = p.message_id
           WHERE m.role = 'assistant'"""
    ).fetchone() == {"n": 0}
    assert retry(client, generation).status_code == 409


def test_chat_us_02_ac15_new_message_after_failure_hides_old_failure(
    client, db, session, monkeypatch
):
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "before")
    failed = send(client, session, [user_part("실패")]).json()
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "none")
    assert send(client, session, [user_part("새 입력")], request_id="next-0001").status_code == 202
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert turns["last_failed_generation"] is None
    assert retry(client, failed["id"]).status_code == 409


def test_chat_us_02_empty_provider_output_is_failed_not_billed(client, db, session, monkeypatch):
    provider = RecordingProvider(chunks=["  ", "\n"])
    monkeypatch.setattr(providers, "get_provider", lambda: provider)
    generation = send(client, session, [user_part("빈 응답")]).json()["id"]
    row = generation_row(db, generation)
    assert row["status"] == "failed"
    assert row["error_code"] == "EMPTY_RESPONSE"
    assert row["billable"] is False


def test_chat_us_02_long_poll_offsets_are_code_points(client, db, session, monkeypatch):
    provider = RecordingProvider(chunks=["@도경: 😀가", "나다"])
    monkeypatch.setattr(providers, "get_provider", lambda: provider)
    generation = send(client, session, [user_part("x")]).json()["id"]
    full = client.get(f"{GENERATIONS}/{generation}", headers=PLAYER).json()
    assert full["text"] == "@도경: 😀가나다"
    assert full["next_offset"] == 9
    tail = client.get(f"{GENERATIONS}/{generation}?offset=5", headers=PLAYER).json()
    assert tail["text"] == "😀가나다"
    assert tail["next_offset"] == 9
    beyond = client.get(f"{GENERATIONS}/{generation}?offset=50", headers=PLAYER).json()
    assert beyond["text"] == ""
    assert beyond["next_offset"] == 9
    assert client.get(f"{GENERATIONS}/{generation}?wait=26", headers=PLAYER).status_code == 422
    assert client.get(f"{GENERATIONS}/{generation}?offset=-1", headers=PLAYER).status_code == 422


def test_chat_us_02_ac13_ac14_thread_worker_streams_and_long_poll_collects_all(
    client, db, session, thread_worker
):
    """실제 스레드 경로: 전송 응답은 바로 pending이고, 롱 폴링으로 끝까지 이어 받는다."""
    sent = send(client, session, [user_part("스레드로 생성")])
    assert sent.status_code == 202
    assert sent.json()["status"] == "pending"
    generation = sent.json()["id"]
    collected = ""
    offset = 0
    statuses = []
    deadline = monotonic() + 20
    while True:
        poll = client.get(
            f"{GENERATIONS}/{generation}?offset={offset}&wait=5", headers=PLAYER
        ).json()
        statuses.append(poll["status"])
        collected += poll["text"]
        offset = poll["next_offset"]
        if poll["status"] not in ("pending", "streaming"):
            break
        assert monotonic() < deadline
    assert poll["status"] == "completed"
    assert "streaming" in statuses
    assert len(statuses) > 1  # 조각마다 여러 번 나눠 받았다.
    assert collected == generation_row(db, generation)["text"]
    assert poll["assistant_message"]["parts"][0]["speaker_name"] == "도경"


def test_chat_us_02_ac13_generation_continues_without_any_client_poll(
    client, db, session, thread_worker
):
    """화면 이탈(조회 없음)에도 서버가 끝까지 생성·저장한다."""
    generation = send(client, session, [user_part("나가도 계속")]).json()["id"]
    deadline = monotonic() + 20
    while generation_row(db, generation)["status"] in ("pending", "streaming"):
        assert monotonic() < deadline
        sleep(0.05)
    row = generation_row(db, generation)
    assert row["status"] == "completed"
    assert row["billable"] is True


def test_chat_us_02_long_poll_waits_then_returns_on_timeout(client, db, session, monkeypatch):
    from app.features.conversation.messaging import worker

    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    generation = send(client, session, [user_part("대기")]).json()["id"]
    started = monotonic()
    poll = client.get(f"{GENERATIONS}/{generation}?wait=0.5", headers=PLAYER).json()
    assert monotonic() - started >= 0.45
    assert poll["status"] == "pending"
    assert poll["text"] == ""


def test_chat_us_02_stale_active_generation_is_expired(client, db, session, monkeypatch):
    from app.features.conversation.messaging import worker

    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    generation = send(client, session, [user_part("서버 중단")]).json()["id"]
    db.execute(
        """UPDATE conversation_generations SET updated_at = now() - interval '1 hour'
           WHERE id = %s""",
        (generation,),
    )
    poll = client.get(f"{GENERATIONS}/{generation}", headers=PLAYER).json()
    assert poll["status"] == "failed"
    assert poll["error_code"] == "GENERATION_STALE"
    assert poll["retryable"] is True
    # 정리된 뒤에는 새 메시지를 보낼 수 있다.
    monkeypatch.undo()
    monkeypatch.setattr(settings, "chat_generation_worker", "inline")
    monkeypatch.setattr(settings, "chat_fake_chunk_delay_ms", 0)
    assert send(client, session, [user_part("다시")], request_id="after-stale").status_code == 202


def test_chat_us_02_worker_does_not_run_twice(client, db, session):
    generation = send(client, session, [user_part("한 번")]).json()["id"]
    before = generation_row(db, generation)
    service.run_generation(generation)
    assert generation_row(db, generation) == before
    assert db.execute("SELECT count(*) AS n FROM conversation_messages").fetchone()["n"] == 2


def test_chat_us_02_fake_provider_markers_and_modes():
    request = ProviderRequest(model="m", messages=[], character_names=["도경"], last_user_text="")
    text = "".join(FakeProvider().stream(request))
    assert text.startswith("@도경: (가짜 응답)")
    with pytest.raises(ProviderError):
        list(FakeProvider(fail_mode="before").stream(request))
    request.last_user_text = "#fail-after"
    received = []
    with pytest.raises(ProviderError) as exc:
        for chunk in FakeProvider().stream(request):
            received.append(chunk)
    assert exc.value.code == "STREAM_INTERRUPTED"
    assert received


def test_chat_us_02_openai_sse_parsing_without_network():
    lines = [
        b": keep-alive\n",
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
        b'data: {"choices":[{"delta":{"content":"@\\ub3c4\\uacbd: "}}]}\n',
        b"\n",
        'data: {"choices":[{"delta":{"content":"안녕"}}]}\n'.encode(),
        b"data: [DONE]\n",
        b'data: {"choices":[{"delta":{"content":"after"}}]}\n',
    ]
    assert "".join(iter_sse_content(lines)) == "@도경: 안녕"
    with pytest.raises(ProviderError):
        list(iter_sse_content([b'data: {"error": {"message": "x"}}']))
    with pytest.raises(ProviderError):
        list(iter_sse_content([b"data: {broken"]))


def test_chat_us_02_openai_provider_requires_base_url_and_key(monkeypatch):
    monkeypatch.setattr(settings, "chat_llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "chat_llm_api_key", "test-key")
    monkeypatch.setattr(settings, "chat_llm_base_url", None)
    with pytest.raises(ProviderError):
        providers.get_provider()
    monkeypatch.setattr(settings, "chat_llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "chat_llm_api_key", None)
    with pytest.raises(ProviderError) as exc:
        providers.get_provider()
    assert exc.value.code == "PROVIDER_NOT_CONFIGURED"
    monkeypatch.setattr(settings, "chat_llm_api_key", "test-key")
    assert isinstance(providers.get_provider(), providers.OpenAICompatibleProvider)


def test_chat_us_02_openai_default_base_url_is_openai():
    assert type(settings).model_fields["chat_llm_base_url"].default == "https://api.openai.com/v1"


def test_chat_us_02_openai_provider_rejects_unsupported_model_provider():
    provider = providers.OpenAICompatibleProvider("http://127.0.0.1:9/v1", "test-key", 1)
    request = ProviderRequest(model="x", messages=[], provider="other")
    with pytest.raises(ProviderError) as exc:
        list(provider.stream(request))
    assert exc.value.code == "PROVIDER_NOT_CONFIGURED"


def test_chat_us_02_openai_provider_sends_model_id_with_configured_prefix(monkeypatch):
    """OpenRouter 같은 중계 서비스는 'openai/gpt-6-luna'처럼 공급자 이름이 붙은 ID를 받는다."""
    sent = []

    def fake_urlopen(http_request, timeout):
        sent.append(json.loads(http_request.data))
        return _FakeHttpResponse(b"".join(_sse(_delta("ok"), "[DONE]")))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(settings, "chat_llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "chat_llm_base_url", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(settings, "chat_llm_api_key", "test-key")
    monkeypatch.setattr(settings, "chat_llm_model_prefix", "openai/")
    provider = providers.get_provider()
    for model in ("gpt-6-luna", "openai/gpt-6-luna"):
        assert "".join(provider.stream(ProviderRequest(model=model, messages=[]))) == "ok"
    assert [body["model"] for body in sent] == ["openai/gpt-6-luna", "openai/gpt-6-luna"]

    # 기본값(접두사 없음)은 DB의 provider_model_id를 그대로 보낸다.
    assert type(settings).model_fields["chat_llm_model_prefix"].default == ""
    plain = providers.OpenAICompatibleProvider("http://127.0.0.1:9/v1", "test-key", 1)
    list(plain.stream(ProviderRequest(model="gpt-6-luna", messages=[])))
    assert sent[-1]["model"] == "gpt-6-luna"


def test_chat_us_02_long_poll_does_not_hold_db_connection_while_waiting(
    client, db, db_url, session, monkeypatch
):
    """롱 폴링은 기다리는 동안 커넥션을 돌려준다: 풀이 1개여도 다른 요청이 바로 끝난다."""
    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    generation = send(client, session, [user_part("대기")]).json()["id"]
    small_pool = ConnectionPool(
        db_url,
        kwargs={"row_factory": dict_row, "autocommit": True},
        min_size=1,
        max_size=1,
        timeout=10,
        open=True,
    )
    monkeypatch.setattr(db_session, "_pool", small_pool)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(client.get, f"{GENERATIONS}/{generation}?wait=3", headers=PLAYER)
            sleep(0.5)
            assert not waiting.done()
            started = monotonic()
            turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER)
            elapsed = monotonic() - started
            assert turns.status_code == 200
            # 롱 폴링이 커넥션을 붙잡았다면 대기가 끝날 때까지(약 2.5초) 막힌다.
            assert elapsed < 1.0
            assert not waiting.done()
            poll = waiting.result(timeout=10)
        assert poll.status_code == 200
        assert poll.json()["status"] == "pending"
    finally:
        small_pool.close()


def _sse(*events: str) -> list[bytes]:
    return [f"data: {event}\n".encode() for event in events] + [b"\n"]


def _delta(text: str) -> str:
    return '{"choices":[{"delta":{"content":"%s"},"finish_reason":null}]}' % text


def test_chat_us_02_sse_requires_completion_signal():
    finish = '{"choices":[{"delta":{},"finish_reason":"stop"}]}'
    # finish_reason만 있고 [DONE]이 없어도 정상 완료다.
    assert "".join(iter_sse_content(_sse(_delta("가"), _delta("나"), finish))) == "가나"
    assert "".join(iter_sse_content(_sse(_delta("가"), "[DONE]"))) == "가"
    received = []
    with pytest.raises(ProviderError) as exc:
        for chunk in iter_sse_content(_sse(_delta("가"), _delta("나"))):
            received.append(chunk)
    assert exc.value.code == "STREAM_INTERRUPTED"
    assert received == ["가", "나"]
    with pytest.raises(ProviderError) as exc:
        list(iter_sse_content([]))
    assert exc.value.code == "STREAM_INTERRUPTED"


class _FakeHttpResponse(io.BytesIO):
    """urlopen 응답 대역. 줄 단위 반복과 with 문을 지원한다(네트워크 없음)."""


def _use_openai_stream(monkeypatch, body: bytes):
    monkeypatch.setattr(settings, "chat_llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "chat_llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "chat_llm_api_key", "test-key")
    monkeypatch.setattr(settings, "chat_stream_flush_ms", 0)
    monkeypatch.setattr(
        providers.urllib.request, "urlopen", lambda *a, **k: _FakeHttpResponse(body)
    )


@pytest.mark.parametrize(
    ("events", "status", "text"),
    [
        ((_delta("@도경: 안녕"), _delta("하세요")), "failed_partial", "@도경: 안녕하세요"),
        ((), "failed", ""),
    ],
)
def test_chat_us_02_ac15_ac17_openai_stream_without_completion_is_not_billed(
    client, db, session, monkeypatch, events, status, text
):
    _use_openai_stream(monkeypatch, b"".join(_sse(*events)))
    generation = send(client, session, [user_part("잘린 응답")]).json()["id"]
    row = generation_row(db, generation)
    assert row["status"] == status
    assert row["error_code"] == "STREAM_INTERRUPTED"
    assert row["text"] == text
    assert row["billable"] is False
    assert row["message_status"] == ("interrupted" if status == "failed_partial" else "failed")


def test_chat_us_02_openai_stream_with_done_completes_and_is_billable(
    client, db, session, monkeypatch
):
    _use_openai_stream(monkeypatch, b"".join(_sse(_delta("@도경: 안녕"), "[DONE]")))
    generation = send(client, session, [user_part("정상 응답")]).json()["id"]
    row = generation_row(db, generation)
    assert row["status"] == "completed"
    assert row["text"] == "@도경: 안녕"
    assert row["billable"] is True


@pytest.mark.parametrize(
    ("chunks", "visible"),
    [
        (["아직 끝나지 않은 답"], True),
        ([""], False),
        (["   "], False),
        (["\ufeff"], False),  # JS trim은 BOM을 지운다.
        (["@나레이터: \x1f"], True),  # JS trim은 이 제어 문자를 지우지 않는다.
        (["@도경:\r\n@나레이터:\x85@Bea:\u2028\ufeff"], False),
        ([" @도경:"], True),  # 태그는 trim 전 줄 맨 앞에서만 인식한다.
        (["@새이름:"], True),  # 세션 시작 후 바뀐 원본 이름은 앱의 태그가 아니다.
        (["@나레", "이터:"], False),
        (["@도", "경: "], False),
        (["@도", "경: 안녕", "\n@나레이터:"], True),
        (["@모르는이:"], True),  # 알 수 없는 태그는 본문으로 표시한다.
    ],
)
def test_chat_us_02_ac17_model_output_limit(
    client, db, session, saved_story, monkeypatch, chunks, visible
):
    _, _, (first, _) = saved_story
    db.execute("UPDATE characters SET name = '새이름' WHERE id = %s", (first,))
    *prefix, last = chunks
    events = [_delta(chunk) for chunk in prefix]
    events.append(
        json.dumps({"choices": [{"delta": {"content": last}, "finish_reason": "length"}]})
    )
    _use_openai_stream(monkeypatch, b"".join(_sse(*events, "[DONE]")))
    generation = send(client, session, [user_part("계속 이야기해줘")]).json()["id"]
    row = generation_row(db, generation)
    assert row["error_code"] == "MODEL_OUTPUT_LIMIT"
    assert row["text"] == "".join(chunks)
    assert row["billable"] is visible
    assert row["status"] == ("failed_partial" if visible else "failed")
    assert row["message_status"] == ("interrupted" if visible else "failed")
    poll = client.get(f"{GENERATIONS}/{generation}", headers=PLAYER).json()
    assert poll["retryable"] is (not visible)
    _use_openai_stream(monkeypatch, b"".join(_sse(_delta("다시 생성한 답"), "[DONE]")))
    response = retry(client, generation)
    if visible:
        assert response.status_code == 409
        assert response.json()["code"] == "GENERATION_NOT_RETRYABLE"
    else:
        assert response.status_code == 202
        assert generation_row(db, response.json()["id"])["status"] == "completed"
