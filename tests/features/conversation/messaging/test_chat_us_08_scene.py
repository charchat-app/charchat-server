"""CHAT-US-08 장면 유지·변화: 응답 맨 끝 ```info 블록 → 다음 요청 [현재 장면](KAN-22).

장면 상태는 따로 저장하지 않는다. 응답 원문(생성 원문)에 남은 info를 다음 요청 때 다시 읽는다.
세션 시작 값(conversation_scene_states·character_states)은 첫 대화에만 쓰고 바꾸지 않는다.
"""

from messaging_helpers import (
    AUTHOR,
    GENERATIONS,
    PLAYER,
    SESSIONS,
    send,
    start_session,
    user_part,
)

from app.features.conversation.messaging import providers, service
from app.features.conversation.messaging.prompt import build_messages
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
    return content.split("\n\n[이용자 입력]")[0].split("\n\n")[-1]


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
    assert request.messages[-1]["content"].endswith(f"{START_SCENE}\n\n[이용자 입력]\n[민서] 안녕.")


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


def test_chat_us_08_info_only_reply_is_empty_not_billed_and_retryable(
    client, db, session, recorder
):
    recorder.chunks = ["\n```info\n", PARK_INFO, "\n```\n"]
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
