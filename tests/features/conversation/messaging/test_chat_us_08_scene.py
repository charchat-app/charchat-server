"""CHAT-US-08 장면 유지·변화: 응답 맨 끝 ```info 블록 → 다음 요청 [현재 장면](KAN-22).

장면 상태는 따로 저장하지 않는다. 응답 원문(생성 원문)에 남은 info를 다음 요청 때 다시 읽는다.
세션 시작 값(conversation_scene_states·character_states)은 첫 대화에만 쓰고 바꾸지 않는다.
"""

import pytest
from messaging_helpers import (
    AUTHOR,
    GENERATIONS,
    PLAYER,
    SESSIONS,
    send,
    start_session,
    user_part,
)

from app.features.conversation.messaging import providers, service, worker
from app.features.conversation.messaging.info import parse_info, scene_view
from app.features.conversation.messaging.prompt import INFO_REMINDER, build_messages
from app.features.conversation.messaging.providers import ProviderError

START_SCENE = (
    "[현재 장면]\n"
    "날짜: 2026년 11월 22일\n"
    "시간: 21:05\n"
    "장소: 경찰서 옥상\n"
    "[도경] 착장: 젖은 코트 | 속마음: 피곤하다\n"
    "[Bea] 착장: - | 속마음: -"
)
SAME_INFO = (
    "날짜: 2026년 11월 22일\n시간: 21:05\n장소: 경찰서 옥상\n"
    "[도경] 착장: 젖은 코트 | 속마음: 피곤하다\n[Bea] 착장: - | 속마음: -"
)
PARK_INFO = (
    "날짜: 2026년 11월 23일\n시간: 12시\n장소: 공원 벤치\n"
    "[도경] 착장: 회색 셔츠 | 속마음: -\n[Bea] 착장: 노란 우비 | 속마음: 배고프다\n"
    "[민서] 착장: 교복 | 속마음: 신난다"
)
PARK_SCENE = (
    "[현재 장면]\n"
    "날짜: 2026년 11월 23일\n"
    "시간: 12시\n"
    "장소: 공원 벤치\n"
    "[도경] 착장: 회색 셔츠 | 속마음: 피곤하다\n"
    "[Bea] 착장: 노란 우비 | 속마음: 배고프다"
)
CAFE_INFO = (
    "날짜: 2026년 11월 24일\n시간: 15:40\n장소: 골목 카페\n"
    "[도경] 착장: 검은 니트 | 속마음: 커피가 쓰다\n[Bea] 착장: - | 속마음: 졸리다"
)
CAFE_SCENE = (
    "[현재 장면]\n"
    "날짜: 2026년 11월 24일\n"
    "시간: 15:40\n"
    "장소: 골목 카페\n"
    "[도경] 착장: 검은 니트 | 속마음: 커피가 쓰다\n"
    "[Bea] 착장: 노란 우비 | 속마음: 졸리다"
)
BODY = "@도경: *고개를 끄덕인다.*\n\n가자.\n@나레이터: *비가 그친다.*"


def reply(info: str | None, body: str = BODY) -> list[str]:
    """응답 조각. info가 있으면 본문 뒤 맨 끝에 info 블록을 붙인다."""
    text = body if info is None else f"{body}\n\n```info\n{info}\n```"
    return [text[:10], text[10:]]


def turn(client, session, recorder, info, text, request_id, headers=PLAYER):
    recorder.chunks = reply(info)
    response = send(client, session, [user_part(text)], request_id=request_id, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


def scene_block(request) -> str:
    """요청 마지막 user 메시지의 [현재 장면] 블록."""
    content = request.messages[-1]["content"]
    return "[현재 장면]\n" + content.split("[현재 장면]\n")[-1].split("\n\n")[0]


def start_rows(db, session_id):
    scene = db.execute(
        "SELECT * FROM conversation_scene_states WHERE session_id = %s", (session_id,)
    ).fetchone()
    characters = db.execute(
        "SELECT * FROM conversation_character_states WHERE session_id = %s ORDER BY id",
        (session_id,),
    ).fetchall()
    return scene, characters


def test_chat_us_08_first_turn_uses_session_start_scene(client, db, session, recorder):
    turn(client, session, recorder, None, "안녕.", "turn-0001")
    (request,) = recorder.requests
    assert scene_block(request) == START_SCENE
    assert request.messages[-1]["content"].endswith(
        f"{START_SCENE}\n\n{INFO_REMINDER}\n\n[이용자 입력]\n[민서] 안녕."
    )


def test_chat_us_08_ac01_same_scene_info_keeps_current_scene(client, db, session, recorder):
    before = start_rows(db, session)
    turn(client, session, recorder, SAME_INFO, "여기 바람이 차네요.", "turn-0001")
    turn(client, session, recorder, SAME_INFO, "조금 더 있을까요?", "turn-0002")
    turn(client, session, recorder, None, "그래요.", "turn-0003")
    assert [scene_block(r) for r in recorder.requests] == [START_SCENE] * 3
    # 세션 시작 값 행은 그대로다(첫 대화용 사본).
    assert start_rows(db, session) == before


def test_chat_us_08_ac02_changed_info_is_next_scene_and_kept_when_reply_has_no_info(
    client, db, session, recorder
):
    before = start_rows(db, session)
    turn(client, session, recorder, PARK_INFO, "다음 날 점심에 공원에서 봐요.", "turn-0001")
    turn(client, session, recorder, None, "배고프다.", "turn-0002")
    turn(client, session, recorder, None, "뭐 먹을까?", "turn-0003")
    first, second, third = recorder.requests
    assert scene_block(first) == START_SCENE
    # 응답 info의 값만 바뀌고, info에 '-'로 둔 속마음은 앞 값(피곤하다)을 잇는다. 이용자 줄은 무시.
    assert scene_block(second) == PARK_SCENE
    # info 없는 응답 다음에도 앞 info 값을 그대로 잇는다.
    assert scene_block(third) == PARK_SCENE
    # [현재 장면]은 최신 입력 앞 한 곳에만 있다. 첫 블록·이력에는 없다.
    contents = [m["content"] for m in third.messages[1:]]
    assert sum("[현재 장면]" in content for content in contents) == 1
    assert all("```info" not in content and "공원 벤치" not in content for content in contents[:-1])
    assert start_rows(db, session) == before


def test_chat_us_08_ac03_scene_survives_reopen_and_model_change(client, db, session, recorder):
    turn(client, session, recorder, PARK_INFO, "다음 날 점심에 공원에서 봐요.", "turn-0001")
    pending = send(client, session, [user_part("계속.")], request_id="turn-0002").json()
    assert scene_block(recorder.requests[-1]) == PARK_SCENE
    # 다시 열기: 세션·대화 기록을 새로 읽어도 같은 장면이 된다(저장된 응답 원문에서 다시 읽는다).
    assert client.get(f"{SESSIONS}/{session}", headers=PLAYER).status_code == 200
    assert client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).status_code == 200
    claimed = db.execute(
        """SELECT g.session_id, g.user_message_id, cs.owner_id FROM conversation_generations g
           JOIN conversation_sessions cs ON cs.id = g.session_id WHERE g.id = %s""",
        (pending["id"],),
    ).fetchone()

    def messages():
        return build_messages(service.load_prompt_context(db, claimed))

    before = messages()
    assert before == recorder.requests[-1].messages
    other = db.execute(
        """SELECT id FROM models WHERE id <> (SELECT selected_model_id FROM conversation_sessions
           WHERE id = %s) ORDER BY id LIMIT 1""",
        (session,),
    ).fetchone()["id"]
    changed = client.put(f"{SESSIONS}/{session}/model", json={"model_id": other}, headers=PLAYER)
    assert changed.status_code == 200, changed.text
    assert messages() == before
    turn(client, session, recorder, None, "모델을 바꿨어요.", "turn-0003")
    assert recorder.requests[-1].model != recorder.requests[0].model
    assert scene_block(recorder.requests[-1]) == PARK_SCENE


def test_chat_us_08_bubbles_have_no_info_and_turns_api_returns_info(client, db, session, recorder):
    generation = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    raw = "".join(reply(PARK_INFO))
    # 원문 전체는 생성 원문에 그대로 남는다(응답 자체에 저장).
    stored = db.execute(
        "SELECT text FROM conversation_generations WHERE id = %s", (generation["id"],)
    ).fetchone()["text"]
    assert stored == raw
    expected_info = {
        "text": PARK_INFO,
        "date": {
            "mode": "calendar",
            "era": None,
            "year": 2026,
            "month": 11,
            "day": 23,
            "reference_event": None,
            "elapsed_day": None,
        },
        "time": {"hour": 12, "minute": None},
        "place": "공원 벤치",
        "characters": [
            {"name": "도경", "clothing": "회색 셔츠", "inner_thought": None},
            {"name": "Bea", "clothing": "노란 우비", "inner_thought": "배고프다"},
        ],
    }
    room = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    user_message, assistant = room["messages"]
    assert user_message["info"] is None
    assert [(p["speaker_name"], p["body"]) for p in assistant["parts"]] == [
        ("도경", "*고개를 끄덕인다.*\n\n가자."),
        (None, "*비가 그친다.*"),
    ]
    assert "```" not in str(assistant["parts"]) and "공원 벤치" not in str(assistant["parts"])
    assert assistant["info"] == expected_info
    # 생성 상태 응답의 완료 메시지에도 같은 info가 있고, text는 원문 그대로다(앱이 숨긴다).
    polled = client.get(f"{GENERATIONS}/{generation['id']}", headers=PLAYER).json()
    assert polled["assistant_message"]["info"] == expected_info
    assert polled["text"] == raw
    # info 없는 응답은 info가 null이다.
    turn(client, session, recorder, None, "응.", "turn-0002")
    room = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert [m["info"] is None for m in room["messages"]] == [True, False, True, True]


def test_chat_us_08_failed_reply_info_is_ignored(client, db, session, recorder):
    # info까지 다 받은 뒤 연결이 끊겼다: 원문에는 info가 있지만 실패(일부) 응답이다.
    recorder.chunks = [*reply(PARK_INFO), "never"]
    recorder.error = ProviderError("STREAM_INTERRUPTED")
    recorder.error_after = 2
    partial = send(client, session, [user_part("공원으로.")], request_id="turn-0001").json()
    recorder.error = recorder.error_after = None
    polled = client.get(f"{GENERATIONS}/{partial['id']}", headers=PLAYER).json()
    assert polled["status"] == "failed_partial"
    assert polled["text"] == "".join(reply(PARK_INFO))
    assert polled["assistant_message"] is None
    turn(client, session, recorder, None, "다시.", "turn-0002")
    assert scene_block(recorder.requests[-1]) == START_SCENE
    room = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert all(m["info"] is None for m in room["messages"])


@pytest.mark.parametrize("body", ["\n", "@도경:\n", "\ufeff \n@나레이터:\n"])
def test_chat_us_08_info_only_reply_is_empty_not_billed_and_retryable(
    client, db, session, recorder, body
):
    # 보이는 글은 앱과 같은 규칙(빈 태그·JS trim)으로 정한다. info만 있으면 빈 응답이다.
    recorder.chunks = [body, "```info\n", PARK_INFO, "\n```\n"]
    generation = send(client, session, [user_part("공원으로.")]).json()
    row = db.execute(
        "SELECT status, billable, error_code FROM conversation_generations WHERE id = %s",
        (generation["id"],),
    ).fetchone()
    assert (row["status"], row["billable"], row["error_code"]) == (
        "failed",
        False,
        "EMPTY_RESPONSE",
    )
    assert client.get(f"{GENERATIONS}/{generation['id']}", headers=PLAYER).json()["retryable"]


def test_chat_us_08_output_limit_with_only_info_is_not_billed(client, db, session, recorder):
    recorder.chunks = ["```info\n장소: 공원 벤치\n", "never"]
    recorder.error = ProviderError(providers.MODEL_OUTPUT_LIMIT)
    recorder.error_after = 1
    generation = send(client, session, [user_part("공원으로.")]).json()
    row = db.execute(
        "SELECT status, billable FROM conversation_generations WHERE id = %s",
        (generation["id"],),
    ).fetchone()
    assert (row["status"], row["billable"]) == ("failed", False)


def test_chat_us_08_output_limit_inside_info_bills_visible_body(client, db, session, recorder):
    # 본문 뒤 info를 쓰다가 한도에 걸렸다: 본문은 보였으므로 청구 대상이다.
    recorder.chunks = [f"{BODY}\n```info\n장소: 공", "never"]
    recorder.error = ProviderError(providers.MODEL_OUTPUT_LIMIT)
    recorder.error_after = 1
    generation = send(client, session, [user_part("공원으로.")]).json()
    row = db.execute(
        "SELECT status, billable FROM conversation_generations WHERE id = %s",
        (generation["id"],),
    ).fetchone()
    assert (row["status"], row["billable"]) == ("failed_partial", True)


def test_chat_us_08_info_stays_in_its_session(client, db, saved_story, session, recorder):
    story, intro, _ = saved_story
    other = start_session(client, story, intro, headers=AUTHOR)
    turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    turn(client, other, recorder, None, "다른 방.", "turn-0002", headers=AUTHOR)
    turn(client, session, recorder, None, "계속.", "turn-0003")
    assert scene_block(recorder.requests[1]) == START_SCENE
    assert scene_block(recorder.requests[2]) == PARK_SCENE
    room = client.get(f"{SESSIONS}/{other}/turns", headers=AUTHOR).json()
    assert all(m["info"] is None for m in room["messages"])


# ---- 리뷰 보강: 요청 재구성·재시도·실패 경로·current_scene·날짜 모양 ----


def claimed_row(db, generation_id):
    return db.execute(
        """SELECT g.session_id, g.user_message_id, cs.owner_id FROM conversation_generations g
           JOIN conversation_sessions cs ON cs.id = g.session_id WHERE g.id = %s""",
        (generation_id,),
    ).fetchone()


def generation_row(db, generation_id):
    return db.execute(
        """SELECT g.status, g.billable, g.error_code, m.status AS message_status
           FROM conversation_generations g
           JOIN conversation_messages m
             ON m.session_id = g.session_id AND m.id = g.assistant_message_id
           WHERE g.id = %s""",
        (generation_id,),
    ).fetchone()


def current_scene(client, session_id, headers=PLAYER):
    response = client.get(f"{SESSIONS}/{session_id}/turns", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["current_scene"]


def calendar_date(year, month, day):
    return {
        "mode": "calendar",
        "era": None,
        "year": year,
        "month": month,
        "day": day,
        "reference_event": None,
        "elapsed_day": None,
    }


def test_chat_us_08_ac03_rebuilt_request_uses_only_replies_before_its_input(
    client, db, session, recorder
):
    # 응답마다 info가 다르다. 앞 생성의 요청을 다시 만들 때 그 뒤 완료 응답이 섞이면 안 된다.
    first = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    second = turn(client, session, recorder, CAFE_INFO, "카페로.", "turn-0002")
    third = turn(client, session, recorder, None, "계속.", "turn-0003")
    assert [scene_block(r) for r in recorder.requests] == [START_SCENE, PARK_SCENE, CAFE_SCENE]
    for generation, request in zip((first, second, third), recorder.requests, strict=True):
        rebuilt = build_messages(service.load_prompt_context(db, claimed_row(db, generation["id"])))
        assert rebuilt == request.messages


@pytest.mark.parametrize(
    "error, code",
    [
        (ProviderError("STREAM_INTERRUPTED"), "STREAM_INTERRUPTED"),
        (RuntimeError("공급자 내부 오류"), "PROVIDER_ERROR"),
    ],
)
def test_chat_us_08_provider_error_after_only_info_is_failed_and_retry_keeps_scene(
    client, db, session, recorder, error, code
):
    turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    park = current_scene(client, session)
    # 본문 없이 info(다른 장면)만 받은 뒤 끊겼다: 보이는 글이 없으므로 빈 실패(재시도 가능)다.
    recorder.chunks = ["\n```info\n", CAFE_INFO, "\n```", "never"]
    recorder.error, recorder.error_after = error, 3
    cut = send(client, session, [user_part("카페로.")], request_id="turn-0002").json()
    recorder.error = recorder.error_after = None
    assert generation_row(db, cut["id"]) == {
        "status": "failed",
        "billable": False,
        "error_code": code,
        "message_status": "failed",
    }
    polled = client.get(f"{GENERATIONS}/{cut['id']}", headers=PLAYER).json()
    assert polled["retryable"] is True
    assert polled["text"] == f"\n```info\n{CAFE_INFO}\n```"
    room = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert room["last_failed_generation"]["id"] == cut["id"]
    assert room["last_failed_generation"]["retryable"] is True
    # 실패 응답의 info는 현재 장면에 들어가지 않는다.
    assert room["current_scene"] == park
    # 재시도(POST …/retry): 같은 이용자 입력으로 다시 요청하고, 장면은 마지막 완료 응답 값이다.
    recorder.chunks = reply(None)
    retried = client.post(
        f"{GENERATIONS}/{cut['id']}/retry", headers=PLAYER, json={"request_id": "retry-0001"}
    )
    assert retried.status_code == 202, retried.text
    assert scene_block(recorder.requests[-1]) == PARK_SCENE
    assert (
        recorder.requests[-1]
        .messages[-1]["content"]
        .endswith(f"{PARK_SCENE}\n\n{INFO_REMINDER}\n\n[이용자 입력]\n[민서] 카페로.")
    )
    assert generation_row(db, retried.json()["id"])["status"] == "completed"
    assert current_scene(client, session) == park


@pytest.mark.parametrize(
    "text, status, message_status, retryable",
    [
        (f"```info\n{CAFE_INFO}\n```", "failed", "failed", True),
        ("\n\n```info\n장소: 골목 카페", "failed", "failed", True),
        ("@도경:\n```info\n장소: 골목 카페\n```", "failed", "failed", True),
        (f"{BODY}\n```info\n장소: 골목 카페", "failed_partial", "interrupted", False),
    ],
)
def test_chat_us_08_stale_generation_is_judged_by_visible_text_not_length(
    client, db, session, monkeypatch, text, status, message_status, retryable
):
    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    generation = send(client, session, [user_part("서버 중단")]).json()["id"]
    db.execute(
        """UPDATE conversation_generations
           SET status = 'streaming', text = %s, updated_at = now() - interval '1 hour'
           WHERE id = %s""",
        (text, generation),
    )
    # 턴 조회가 멈춘 생성을 정리한다(전송·재시도·생성 조회도 같은 함수를 쓴다).
    room = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert room["active_generation"] is None
    failed = room["last_failed_generation"]
    assert (failed["id"], failed["status"], failed["error_code"], failed["retryable"]) == (
        generation,
        status,
        "GENERATION_STALE",
        retryable,
    )
    assert generation_row(db, generation) == {
        "status": status,
        "billable": False,
        "error_code": "GENERATION_STALE",
        "message_status": message_status,
    }


def test_chat_us_08_stale_generation_is_expired_on_poll_too(client, db, session, monkeypatch):
    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    generation = send(client, session, [user_part("서버 중단")]).json()["id"]
    db.execute(
        """UPDATE conversation_generations
           SET status = 'streaming', text = %s, updated_at = now() - interval '1 hour'
           WHERE id = %s""",
        (f"```info\n{CAFE_INFO}\n```", generation),
    )
    polled = client.get(f"{GENERATIONS}/{generation}", headers=PLAYER).json()
    assert (polled["status"], polled["retryable"]) == ("failed", True)


@pytest.mark.parametrize(
    "chunks, status",
    [
        (["```info\n장소: 골목 카페\n```"], "failed"),
        ([f"{BODY}\n```info\n장소: 골목 카페\n```"], "failed_partial"),
    ],
)
def test_chat_us_08_internal_error_is_judged_by_visible_text(
    client, db, session, recorder, monkeypatch, chunks, status
):
    def broken(*args, **kwargs):
        raise RuntimeError("실패 기록 중 오류")

    recorder.chunks = [*chunks, "never"]
    recorder.error, recorder.error_after = ProviderError("STREAM_INTERRUPTED"), len(chunks)
    monkeypatch.setattr(service, "_fail", broken)
    generation = send(client, session, [user_part("카페로.")]).json()["id"]
    row = generation_row(db, generation)
    assert (row["status"], row["error_code"], row["billable"]) == (status, "INTERNAL_ERROR", False)
    polled = client.get(f"{GENERATIONS}/{generation}", headers=PLAYER).json()
    assert polled["retryable"] is (status == "failed")


START_VIEW = {
    "date": calendar_date(2026, 11, 22),
    "time": {"hour": 21, "minute": 5},
    "place": "경찰서 옥상",
    "characters": [
        {"name": "도경", "clothing": "젖은 코트", "inner_thought": "피곤하다"},
        {"name": "Bea", "clothing": None, "inner_thought": None},
    ],
}
PARK_VIEW = {
    "date": calendar_date(2026, 11, 23),
    "time": {"hour": 12, "minute": None},
    "place": "공원 벤치",
    "characters": [
        {"name": "도경", "clothing": "회색 셔츠", "inner_thought": "피곤하다"},
        {"name": "Bea", "clothing": "노란 우비", "inner_thought": "배고프다"},
    ],
}
CAFE_VIEW = {
    "date": calendar_date(2026, 11, 24),
    "time": {"hour": 15, "minute": 40},
    "place": "골목 카페",
    "characters": [
        {"name": "도경", "clothing": "검은 니트", "inner_thought": "커피가 쓰다"},
        {"name": "Bea", "clothing": "노란 우비", "inner_thought": "졸리다"},
    ],
}


def test_chat_us_08_turns_current_scene_is_what_the_next_request_is_built_from(
    client, db, session, recorder, monkeypatch
):
    contexts = []
    load = service.load_prompt_context

    def spy(conn, generation):
        contexts.append(context := load(conn, generation))
        return context

    monkeypatch.setattr(service, "load_prompt_context", spy)
    seen = []
    for info, text, request_id in (
        (None, "안녕.", "turn-0001"),
        (PARK_INFO, "공원으로.", "turn-0002"),
        (None, "배고파.", "turn-0003"),
        (CAFE_INFO, "카페로.", "turn-0004"),
        (None, "계속.", "turn-0005"),
    ):
        seen.append(current_scene(client, session))
        turn(client, session, recorder, info, text, request_id)
    seen.append(current_scene(client, session))
    assert seen == [START_VIEW, START_VIEW, PARK_VIEW, PARK_VIEW, CAFE_VIEW, CAFE_VIEW]
    # 턴 조회 current_scene = 바로 다음 요청의 [현재 장면]이 만들어진 값(같은 함수에서 나온다).
    assert seen[:-1] == [scene_view(c.scene, c.character_states) for c in contexts]
    for view, request in zip(seen[:-1], recorder.requests, strict=True):
        block = parse_info(
            scene_block(request).removeprefix("[현재 장면]\n"), "calendar", ["도경", "Bea"]
        )
        assert {key: view[key] for key in ("date", "time", "place")} == {
            key: block[key] for key in ("date", "time", "place")
        }
        assert [c for c in view["characters"] if c["clothing"] or c["inner_thought"]] == block[
            "characters"
        ]


@pytest.mark.parametrize(
    "mode, start, info_line, info_date, scene_date, scene_line",
    [
        (
            "world",
            {"era": "대한", "year": 1898, "month": 3, "day": 1},
            "날짜: 대한 1898년 3월 2일",
            {"era": "대한", "year": 1898, "month": 3, "day": 2},
            {"era": "대한", "year": 1898, "month": 3, "day": 2},
            "날짜: 대한 1898년 3월 2일",
        ),
        (
            "elapsed",
            {"reference_event": "감금", "elapsed_day": 3},
            "날짜: 4일째",
            {"reference_event": None, "elapsed_day": 4},
            {"reference_event": "감금", "elapsed_day": 4},
            "날짜: 감금 4일째",
        ),
        (
            "elapsed",
            {"reference_event": "감금", "elapsed_day": 3},
            "날짜: 탈출 1일째",
            {"reference_event": "탈출", "elapsed_day": 1},
            {"reference_event": "탈출", "elapsed_day": 1},
            "날짜: 탈출 1일째",
        ),
    ],
)
def test_chat_us_08_world_and_elapsed_dates_in_api(
    client, db, session, recorder, mode, start, info_line, info_date, scene_date, scene_line
):
    columns = ("era", "year", "month", "day", "reference_event", "elapsed_day")
    values = {key: start.get(key) for key in columns}
    db.execute(
        """UPDATE conversation_scene_states
           SET date_mode = %s, era = %s, year = %s, month = %s, day = %s,
               reference_event = %s, elapsed_day = %s
           WHERE session_id = %s""",
        (mode, *values.values(), session),
    )
    blank = dict.fromkeys(columns)
    assert current_scene(client, session)["date"] == {"mode": mode, **blank, **start}
    turn(client, session, recorder, f"{info_line}\n장소: 공원 벤치", "공원으로.", "turn-0001")
    room = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert room["messages"][1]["info"]["date"] == {"mode": mode, **blank, **info_date}
    assert room["current_scene"]["date"] == {"mode": mode, **blank, **scene_date}
    assert room["current_scene"]["place"] == "공원 벤치"
    turn(client, session, recorder, None, "계속.", "turn-0002")
    assert f"[현재 장면]\n{scene_line}\n" in recorder.requests[-1].messages[-1]["content"]


def test_chat_us_08_placeholder_info_keeps_previous_scene(client, db, session, recorder):
    # 모델이 형식 예시의 <...>나 '미정'을 그대로 옮겨도 장면은 바뀌지 않는다.
    copied = (
        "날짜: <연>년 <월>월 <일>일\n시간: <시>:<분>\n장소: <장소>\n"
        "[도경] 착장: <착장> | 속마음: 미정\n[Bea] 착장: 모름 | 속마음: <속마음>"
    )
    turn(client, session, recorder, copied, "그대로?", "turn-0001")
    turn(client, session, recorder, None, "응.", "turn-0002")
    assert scene_block(recorder.requests[-1]) == START_SCENE
    room = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert room["current_scene"] == START_VIEW
    info = room["messages"][1]["info"]
    assert (info["date"], info["time"], info["place"], info["characters"]) == (None, None, None, [])
