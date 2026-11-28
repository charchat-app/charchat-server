"""CHAT-US-13 대화 중 모델 변경: 모델 목록·변경, 요청·실제 모델 기록, 자동 대체 없음."""

import io
import json
import threading
import time

import psycopg
import pytest
from model_change_helpers import (
    AUTHOR,
    GENERATIONS,
    MODELS,
    PLAYER,
    SESSIONS,
    RecordingProvider,
    change_model,
    generation_row,
    model_id,
    send,
    turns,
)

from app.core.config import settings
from app.features.conversation.messaging import providers, service
from app.features.conversation.messaging.prompt import build_messages
from app.features.conversation.messaging.providers import (
    FakeProvider,
    ProviderError,
    ProviderRequest,
    iter_sse_content,
)

LUNA_PROVIDER_ID = "gpt-6-luna"
TERRA_PROVIDER_ID = "gpt-6.1-terra"


def retry(client, generation_id, request_id="retry-00000001"):
    return client.post(
        f"{GENERATIONS}/{generation_id}/retry", headers=PLAYER, json={"request_id": request_id}
    )


def selected_model(db, session_id):
    return db.execute(
        "SELECT selected_model_id FROM conversation_sessions WHERE id = %s", (session_id,)
    ).fetchone()["selected_model_id"]


def generation_count(db, session_id):
    return db.execute(
        "SELECT count(*) AS n FROM conversation_generations WHERE session_id = %s", (session_id,)
    ).fetchone()["n"]


# ---- GET /conversation/models ----


def test_chat_us_13_ac01_lists_supported_models_with_default(client, db):
    luna, terra = model_id(db, "루나"), model_id(db, "테라")
    response = client.get(MODELS, headers=PLAYER)
    assert response.status_code == 200
    # id 오름차순, 공급자·공급자 쪽 모델 ID는 노출하지 않는다.
    assert response.json() == {
        "models": [{"id": luna, "name": "루나"}, {"id": terra, "name": "테라"}],
        "default_model_id": luna,
    }
    assert LUNA_PROVIDER_ID not in response.text


def test_chat_us_13_ac01_models_requires_user(client):
    response = client.get(MODELS)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


def test_chat_us_13_ac01_default_model_id_is_null_when_missing_or_duplicated(
    client, db, monkeypatch
):
    monkeypatch.setattr(settings, "chat_default_model_name", "없는 모델")
    assert client.get(MODELS, headers=PLAYER).json()["default_model_id"] is None
    monkeypatch.setattr(settings, "chat_default_model_name", "루나")
    extra = db.execute(
        "INSERT INTO models (name, provider, provider_model_id) "
        "VALUES ('루나', 'openai', 'dup') RETURNING id"
    ).fetchone()["id"]
    try:
        body = client.get(MODELS, headers=PLAYER).json()
        assert body["default_model_id"] is None
        assert [m["name"] for m in body["models"]] == ["루나", "테라", "루나"]
    finally:
        # models는 테스트마다 비우지 않는 기준 데이터라 직접 되돌린다.
        db.execute("DELETE FROM models WHERE id = %s", (extra,))


# ---- PUT /conversation/sessions/{id}/model ----


def test_chat_us_13_ac01_change_model_then_next_send_uses_selected_model(
    client, db, session, recorder
):
    terra = model_id(db, "테라")
    assert turns(client, session)["model_id"] == model_id(db, "루나")

    response = change_model(client, session, terra)
    assert response.status_code == 200
    assert response.json() == {"model_id": terra, "model": "테라", "active_generation": None}
    room = turns(client, session)
    assert (room["model"], room["model_id"]) == ("테라", terra)
    # 내 대화 목록(CHAT-US-03)도 바뀐 모델 이름을 보여 준다.
    listed = client.get(SESSIONS, headers=PLAYER).json()["sessions"]
    assert [row["model"] for row in listed] == ["테라"]

    sent = send(client, session, "모델을 바꾼 뒤 첫 메시지")
    assert sent.status_code == 202
    assert sent.json()["model"] == "테라"
    (request,) = recorder.requests
    assert (request.provider, request.model) == ("openai", TERRA_PROVIDER_ID)
    row = generation_row(db, sent.json()["id"])
    assert (row["status"], row["model_id"]) == ("completed", terra)
    polled = client.get(f"{GENERATIONS}/{sent.json()['id']}", headers=PLAYER).json()
    assert (polled["status"], polled["model"]) == ("completed", "테라")


def test_chat_us_13_ac01_same_model_change_is_idempotent(client, db, session):
    luna, terra = model_id(db, "루나"), model_id(db, "테라")
    for _ in range(2):
        response = change_model(client, session, luna)
        assert response.status_code == 200
        assert response.json() == {"model_id": luna, "model": "루나", "active_generation": None}
        assert selected_model(db, session) == luna
    for _ in range(2):
        assert change_model(client, session, terra).json()["model_id"] == terra
        assert selected_model(db, session) == terra


@pytest.mark.parametrize(
    ("target", "headers"),
    [("own", AUTHOR), ("unknown", PLAYER), ("huge", PLAYER)],
)
def test_chat_us_13_change_model_hides_other_owner_and_unknown_session(
    client, db, session, target, headers
):
    luna = model_id(db, "루나")
    session_id = {"own": session, "unknown": session + 1000, "huge": 2**63}[target]
    response = change_model(client, session_id, model_id(db, "테라"), headers=headers)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"
    assert selected_model(db, session) == luna


def test_chat_us_13_change_model_hides_session_of_inaccessible_story(client, db, session):
    # 작성자(1)의 비공개 스토리 세션은 이용자(2)에게 접근 불가(get_session 규칙과 같음).
    # 현재 제작 정책에서 금지된 공개→비공개 상태는 테스트 데이터로만 재현한다(CHAT-US-03과 같음).
    with db.transaction():
        db.execute("ALTER TABLE stories DISABLE TRIGGER stories_lifecycle_guard")
        db.execute("UPDATE stories SET save_state = 'private'")
        db.execute("ALTER TABLE stories ENABLE TRIGGER stories_lifecycle_guard")
    response = change_model(client, session, model_id(db, "테라"))
    assert response.status_code == 404
    assert response.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"


def test_chat_us_13_change_model_rejects_read_only_session(client, db, session):
    luna = model_id(db, "루나")
    db.execute("UPDATE stories SET deleted_at = now()")
    response = change_model(client, session, model_id(db, "테라"))
    assert response.status_code == 409
    assert response.json()["code"] == "CONVERSATION_READ_ONLY"
    assert selected_model(db, session) == luna
    # 읽기 전용 세션도 조회는 되고 모델은 그대로다.
    assert turns(client, session)["model"] == "루나"


@pytest.mark.parametrize("value", [999_999, 2**63])
def test_chat_us_13_change_model_rejects_unknown_model(client, db, session, value):
    response = change_model(client, session, value)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "CONVERSATION_MODEL_NOT_FOUND"
    assert selected_model(db, session) == model_id(db, "루나")


@pytest.mark.parametrize(
    "body",
    [
        {"model_id": "2"},
        {"model_id": 0},
        {"model_id": -1},
        {"model_id": 1.5},
        {"model_id": True},
        {},
    ],
)
def test_chat_us_13_change_model_validates_positive_integer(client, db, session, body):
    response = client.put(f"{SESSIONS}/{session}/model", headers=PLAYER, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert selected_model(db, session) == model_id(db, "루나")


def test_chat_us_13_change_model_requires_user(client, session):
    response = client.put(f"{SESSIONS}/{session}/model", json={"model_id": 1})
    assert response.status_code == 401


# ---- AC02 진행 중 생성은 기존 모델 그대로 ----


def test_chat_us_13_ac02_change_while_pending_keeps_old_model_for_active_generation(
    client, db, session, recorder, deferred
):
    luna, terra = model_id(db, "루나"), model_id(db, "테라")
    sent = send(client, session, "아직 시작 전인 요청")
    generation = sent.json()["id"]
    assert sent.json()["status"] == "pending"

    response = change_model(client, session, terra)
    assert response.status_code == 200
    assert response.json() == {
        "model_id": terra,
        "model": "테라",
        "active_generation": {"id": generation, "model": "루나"},
    }
    room = turns(client, session)
    assert (room["model"], room["model_id"]) == ("테라", terra)
    assert room["active_generation"]["model"] == "루나"
    assert generation_row(db, generation)["model_id"] == luna

    deferred.run()
    assert [r.model for r in recorder.requests] == [LUNA_PROVIDER_ID]
    row = generation_row(db, generation)
    assert (row["status"], row["model_id"]) == ("completed", luna)
    polled = client.get(f"{GENERATIONS}/{generation}", headers=PLAYER).json()
    assert polled["model"] == "루나"

    # 다음 요청부터 새 모델로 요청한다.
    nxt = send(client, session, "다음 요청", request_id="req-00000002")
    deferred.run()
    assert [r.model for r in recorder.requests] == [LUNA_PROVIDER_ID, TERRA_PROVIDER_ID]
    assert generation_row(db, nxt.json()["id"])["model_id"] == terra
    assert nxt.json()["model"] == "테라"


class _SwitchMidStream(FakeProvider):
    """첫 조각을 보낸 뒤(생성이 streaming인 동안) 모델 변경 요청을 보내는 가짜 공급자."""

    def __init__(self, on_first_chunk):
        super().__init__()
        self.on_first_chunk = on_first_chunk
        self.models = []

    def stream(self, request):
        self.models.append(request.model)
        for index, chunk in enumerate(super().stream(request)):
            yield chunk
            if index == 0 and len(self.models) == 1:
                self.on_first_chunk()


def test_chat_us_13_ac02_change_while_streaming_finishes_with_old_model(
    client, db, session, deferred, monkeypatch
):
    luna, terra = model_id(db, "루나"), model_id(db, "테라")
    seen = {}

    def switch():
        seen["status"] = generation_row(db, generation)["status"]
        seen["put"] = change_model(client, session, terra).json()
        seen["turns"] = turns(client, session)

    provider = _SwitchMidStream(switch)
    monkeypatch.setattr(providers, "get_provider", lambda: provider)
    generation = send(client, session, "길게 답해 줘").json()["id"]
    deferred.run()

    assert seen["status"] == "streaming"
    assert seen["put"] == {
        "model_id": terra,
        "model": "테라",
        "active_generation": {"id": generation, "model": "루나"},
    }
    assert seen["turns"]["model"] == "테라"
    assert seen["turns"]["active_generation"]["model"] == "루나"
    assert provider.models == [LUNA_PROVIDER_ID]
    row = generation_row(db, generation)
    assert row == {
        "status": "completed",
        "model_id": luna,
        "reported_model": LUNA_PROVIDER_ID,
        "error_code": None,
        "text": row["text"],
    }
    assert f"가짜 모델({LUNA_PROVIDER_ID})" in row["text"]

    send(client, session, "다음", request_id="req-00000002")
    deferred.run()
    assert provider.models == [LUNA_PROVIDER_ID, TERRA_PROVIDER_ID]


# ---- AC01 실제 모델 기록 ----


@pytest.mark.parametrize(
    ("body", "status", "reported"),
    [
        ("정상 응답", "completed", TERRA_PROVIDER_ID),
        ("#fail-after 일부만", "failed_partial", TERRA_PROVIDER_ID),
        ("#fail-before 시작 전 실패", "failed", None),
    ],
)
def test_chat_us_13_ac01_records_reported_model_from_fake_provider(
    client, db, session, body, status, reported
):
    terra = model_id(db, "테라")
    change_model(client, session, terra)
    generation = send(client, session, body).json()["id"]
    row = generation_row(db, generation)
    assert (row["status"], row["model_id"], row["reported_model"]) == (status, terra, reported)


def test_chat_us_13_ac01_reported_model_is_null_when_provider_never_reports(
    client, db, session, recorder
):
    generation = send(client, session, "알려 주지 않는 공급자").json()["id"]
    row = generation_row(db, generation)
    assert (row["status"], row["reported_model"]) == ("completed", None)


def test_chat_us_13_ac01_records_reported_model_on_output_limit_and_late_failure(
    client, db, session, monkeypatch
):
    class Reporting:
        def __init__(self, error):
            self.error = error

        def stream(self, request):
            request.report_model("served-model-2026")
            yield "@도경: 길게 이어지는 "
            raise self.error

    for index, error in enumerate(
        [ProviderError(providers.MODEL_OUTPUT_LIMIT), ProviderError("STREAM_INTERRUPTED")]
    ):
        provider = Reporting(error)
        monkeypatch.setattr(providers, "get_provider", lambda p=provider: p)
        generation = send(client, session, "길게", request_id=f"req-limit-{index}").json()["id"]
        row = generation_row(db, generation)
        assert row["status"] == "failed_partial"
        assert row["error_code"] == error.code
        assert row["reported_model"] == "served-model-2026"


def _sse(*payloads) -> list[bytes]:
    return [f"data: {json.dumps(p) if isinstance(p, dict) else p}\n".encode() for p in payloads]


def _chunk(text, model="gpt-6.1-terra-2026-11-01", finish=None):
    return {
        "model": model,
        "choices": [{"delta": {"content": text} if text else {}, "finish_reason": finish}],
    }


def test_chat_us_13_ac01_sse_parser_reports_payload_model():
    request = ProviderRequest(model=TERRA_PROVIDER_ID, messages=[])
    lines = _sse(
        _chunk("가"), _chunk("나", model="other-later"), _chunk("", finish="stop"), "[DONE]"
    )
    assert "".join(iter_sse_content(lines, on_model=request.report_model)) == "가나"
    # 처음 받은 값만 기억한다.
    assert request.reported_model == "gpt-6.1-terra-2026-11-01"

    seen = []
    assert (
        "".join(
            iter_sse_content(
                _sse({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}),
                on_model=seen.append,
            )
        )
        == "x"
    )
    assert seen == []


def test_chat_us_13_ac01_reported_model_skips_unstorable_values():
    # NUL이 든 값은 PostgreSQL text에 저장할 수 없고, 지나치게 긴 값은 기록하지 않는다.
    request = ProviderRequest(model=TERRA_PROVIDER_ID, messages=[])
    for bad in ("gpt\x00x", "m" * 201, "", None, 7):
        request.report_model(bad)
        assert request.reported_model is None
    request.report_model("m" * 200)
    assert request.reported_model == "m" * 200


class _FakeHttpResponse(io.BytesIO):
    """urlopen 응답 대역(네트워크 없음)."""


def test_chat_us_13_ac01_openai_stream_records_served_model(client, db, session, monkeypatch):
    sent_bodies = []

    def fake_urlopen(http_request, timeout):
        sent_bodies.append(json.loads(http_request.data))
        body = b"".join(_sse(_chunk("@도경: 안녕"), _chunk("", finish="stop"), "[DONE]"))
        return _FakeHttpResponse(body)

    monkeypatch.setattr(settings, "chat_llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "chat_llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "chat_llm_api_key", "test-key")
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    change_model(client, session, model_id(db, "테라"))

    generation = send(client, session, "실제 모델 확인").json()["id"]
    assert [body["model"] for body in sent_bodies] == [TERRA_PROVIDER_ID]
    row = generation_row(db, generation)
    assert row["status"] == "completed"
    assert (row["model_id"], row["reported_model"]) == (
        model_id(db, "테라"),
        "gpt-6.1-terra-2026-11-01",
    )


# ---- AC03 실패를 숨기지 않고 몰래 다른 모델로 바꾸지 않음 ----


@pytest.mark.parametrize(("error_after", "status"), [(None, "failed"), (1, "failed_partial")])
def test_chat_us_13_ac03_failed_model_is_not_silently_replaced(
    client, db, session, monkeypatch, error_after, status
):
    terra = model_id(db, "테라")
    change_model(client, session, terra)
    provider = RecordingProvider(error=ProviderError("PROVIDER_ERROR"), error_after=error_after)
    monkeypatch.setattr(providers, "get_provider", lambda: provider)

    generation = send(client, session, "테라가 실패하는 요청").json()["id"]

    assert [r.model for r in provider.requests] == [TERRA_PROVIDER_ID]
    assert generation_count(db, session) == 1
    row = generation_row(db, generation)
    assert (row["status"], row["model_id"], row["error_code"]) == (
        status,
        terra,
        "PROVIDER_ERROR",
    )
    polled = client.get(f"{GENERATIONS}/{generation}", headers=PLAYER).json()
    assert (polled["status"], polled["model"]) == (status, "테라")
    failed = turns(client, session)["last_failed_generation"]
    assert (failed["id"], failed["model"]) == (generation, "테라")
    assert selected_model(db, session) == terra


def test_chat_us_13_ac03_retry_after_switch_uses_current_model_and_same_prompt(
    client, db, session, recorder
):
    luna, terra = model_id(db, "루나"), model_id(db, "테라")
    recorder.error = ProviderError("PROVIDER_ERROR")
    failed = send(client, session, "루나로 실패").json()
    assert failed["model"] == "루나"
    assert generation_row(db, failed["id"])["status"] == "failed"

    assert change_model(client, session, terra).json()["active_generation"] is None
    recorder.error = None
    retried = retry(client, failed["id"])
    assert retried.status_code == 202
    assert retried.json()["model"] == "테라"

    first, second = recorder.requests
    assert (first.model, second.model) == (LUNA_PROVIDER_ID, TERRA_PROVIDER_ID)
    # 모델만 바뀌고 캐릭터·관계·장면을 담은 프롬프트는 같다(AC03).
    assert second.messages == first.messages
    prompt = "\n".join(m["content"] for m in second.messages)
    for expected in (
        "- 도경: 무뚝뚝한 형사",
        "관계 파트너",
        "장소: 경찰서 옥상",
        "[도경] 착장: 젖은 코트",
    ):
        assert expected in prompt
    assert generation_row(db, failed["id"])["model_id"] == luna
    assert generation_row(db, retried.json()["id"])["model_id"] == terra
    old = client.get(f"{GENERATIONS}/{failed['id']}", headers=PLAYER).json()
    assert old["model"] == "루나"
    assert generation_count(db, session) == 2


# ---- 모델 변경이 커밋되는 동안 기다리던 전송·재시도 ----


def _run_while_model_change_commits(db_url, session_id, new_model, call):
    """다른 연결이 세션 행을 잠그고 모델을 바꾼 채 있는 동안 call()을 보내고, 잠시 뒤 커밋한다.

    call()은 세션 행 잠금에서 기다렸다가 커밋 뒤에 풀린다. 반환: call()의 응답.
    """
    result = {}
    with psycopg.connect(db_url) as other:
        other.execute(
            "UPDATE conversation_sessions SET selected_model_id = %s WHERE id = %s",
            (new_model, session_id),
        )
        thread = threading.Thread(target=lambda: result.setdefault("response", call()))
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive(), "전송·재시도가 세션 행 잠금에서 기다리지 않았다"
        other.commit()
    thread.join(timeout=10)
    assert not thread.is_alive()
    return result["response"]


def test_chat_us_13_ac01_send_waiting_on_model_change_uses_new_model(
    client, db, db_url, session, recorder
):
    terra = model_id(db, "테라")

    sent = _run_while_model_change_commits(
        db_url, session, terra, lambda: send(client, session, "바꾸는 중에 보낸 메시지")
    )

    # 바뀐 모델의 조인 때문에 세션을 못 찾아 404가 나면 안 된다.
    assert sent.status_code == 202, sent.text
    assert sent.json()["model"] == "테라"
    assert generation_row(db, sent.json()["id"])["model_id"] == terra
    (request,) = recorder.requests
    assert request.model == TERRA_PROVIDER_ID


def test_chat_us_13_ac03_retry_waiting_on_model_change_uses_new_model(
    client, db, db_url, session, recorder
):
    terra = model_id(db, "테라")
    recorder.error = ProviderError("PROVIDER_ERROR")
    failed = send(client, session, "루나로 실패").json()
    assert generation_row(db, failed["id"])["status"] == "failed"
    recorder.error = None

    retried = _run_while_model_change_commits(
        db_url, session, terra, lambda: retry(client, failed["id"])
    )

    assert retried.status_code == 202, retried.text
    assert retried.json()["model"] == "테라"
    assert generation_row(db, retried.json()["id"])["model_id"] == terra
    assert [r.model for r in recorder.requests] == [LUNA_PROVIDER_ID, TERRA_PROVIDER_ID]


def test_chat_us_13_ac03_build_messages_identical_before_and_after_model_switch(
    client, db, session, deferred
):
    # 이력·장면 변화가 있는 상태를 만든다.
    send(client, session, "첫 대화")
    deferred.run()
    db.execute("UPDATE conversation_scene_states SET place = '지하 주차장'")
    pending = send(client, session, "두 번째 대화", request_id="req-00000002").json()
    # 생성 작업이 받는 claimed 행과 같은 키(session_id·user_message_id·owner_id).
    claimed = db.execute(
        """SELECT g.session_id, g.user_message_id, cs.owner_id
           FROM conversation_generations g
           JOIN conversation_sessions cs ON cs.id = g.session_id WHERE g.id = %s""",
        (pending["id"],),
    ).fetchone()

    def messages():
        return build_messages(service.load_prompt_context(db, claimed))

    before = messages()
    change_model(client, session, model_id(db, "테라"))
    after = messages()
    assert after == before
    prompt = "\n".join(m["content"] for m in after)
    for expected in ("장소: 지하 주차장", "관계 파트너", "속마음: 피곤하다", "[민서] 첫 대화"):
        assert expected in prompt


# ---- 응답 필드 ----


def test_chat_us_13_generation_model_in_request_id_lookup_and_turns(client, db, session, deferred):
    luna, terra = model_id(db, "루나"), model_id(db, "테라")
    sent = send(client, session, "조회용")
    lookup = client.get(
        f"{SESSIONS}/{session}/generations",
        headers=PLAYER,
        params={"request_id": "req-00000001"},
    ).json()["generations"]
    assert [(g["id"], g["status"], g["model"]) for g in lookup] == [
        (sent.json()["id"], "pending", "루나")
    ]
    change_model(client, session, terra)
    deferred.run()
    lookup = client.get(
        f"{SESSIONS}/{session}/generations",
        headers=PLAYER,
        params={"request_id": "req-00000001"},
    ).json()["generations"]
    assert [(g["status"], g["model"]) for g in lookup] == [("completed", "루나")]
    room = turns(client, session)
    assert (room["model"], room["model_id"]) == ("테라", terra)
    assert room["active_generation"] is None and room["last_failed_generation"] is None
    assert luna != terra
